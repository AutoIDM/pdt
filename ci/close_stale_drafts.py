"""Close Draft merge requests that nobody has touched for a while.

A scheduled GitLab pipeline runs this once a day. It lists the project's open
Draft merge requests, and any one that no person has touched for more than
STALE_BUSINESS_DAYS business days gets the comment CLOSE_COMMENT, is closed,
and is listed in one message to Slack.

"Touched by a person" means the MR was opened, a commit was authored, or a
note (comment or system note such as a push) was written by anyone other than
the user behind GITLAB_TOKEN. Every CI job in this project writes through that
token, so a rebase by ``rebase-mrs``, a label or comment from ``score-mrs``,
or any other automated change does not keep a Draft alive. A rebase keeps each
commit's author date, so pushed-again commits do not count either. The MR's
``updated_at`` is not used, because GitLab bumps it for those changes too.

Environment:
  GITLAB_TOKEN        project access token with the ``api`` scope, sent as a
                      bearer token. The CI job token cannot comment on or
                      close merge requests.
  SLACK_WEBHOOK_URL   Slack incoming webhook. When unset the message is
                      printed instead of posted.
  DRY_RUN             anything but the exact word ``false`` means list the
                      candidates and change nothing. A merge request pipeline
                      is always a dry run, whatever DRY_RUN says.
  CI_API_V4_URL, CI_PROJECT_ID, CI_PROJECT_PATH, CI_PIPELINE_SOURCE
                      set by GitLab CI.

Business days are Monday to Friday. There is no holiday calendar.
Stdlib only, so CI runs it with no extra install.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

STALE_BUSINESS_DAYS = 5
CLOSE_COMMENT = "Stale Draft MR, closing"
TIMEOUT = 60
DEFAULT_API = "https://gitlab.com/api/v4"


class HttpFailure(Exception):
    """An HTTP call answered with an error status."""


@dataclass
class Settings:
    api_url: str
    project_id: str
    project_path: str
    token: str
    webhook: str
    dry_run: bool


def business_days_since(then: date, today: date) -> int:
    """Count Monday-to-Friday dates after ``then`` up to and including ``today``."""
    count = 0
    day = then
    while day < today:
        day += timedelta(days=1)
        if day.weekday() < 5:
            count += 1
    return count


def utc_date(stamp: str) -> date:
    return datetime.fromisoformat(stamp).astimezone(timezone.utc).date()


def last_person_activity(mr: dict, commits: list[dict], notes: list[dict],
                         bot_id: int) -> date:
    """The latest date a person touched the MR.

    Counts the MR being opened, each commit's author date (a rebase rewrites
    the committer date but keeps the author date), and each note whose author
    is not the bot behind GITLAB_TOKEN. System notes count too: a person's
    push, retitle, or mark-as-ready shows up as one.
    """
    stamps = [mr["created_at"]]
    stamps += [commit["authored_date"] for commit in commits]
    stamps += [note["updated_at"] for note in notes
               if note.get("author", {}).get("id") != bot_id]
    return max(utc_date(stamp) for stamp in stamps)


def is_stale(mr: dict, last_active: date, today: date) -> bool:
    return bool(mr.get("draft")) and (
        business_days_since(last_active, today) > STALE_BUSINESS_DAYS)


def http(method: str, url: str, headers: dict, payload: dict | None = None):
    """One HTTP call. Returns (parsed body, response headers)."""
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        url, method=method, data=data,
        headers={**headers, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            body = response.read()
    except urllib.error.HTTPError as error:
        detail = error.read().decode(errors="replace")[:500]
        raise HttpFailure(f"{method} {url} returned {error.code}: {detail}") from None
    parsed = json.loads(body) if body.startswith((b"{", b"[")) else body.decode()
    return parsed, response.headers


def api(settings: Settings, method: str, path: str, payload: dict | None = None):
    return http(method, f"{settings.api_url}{path}",
                {"Authorization": f"Bearer {settings.token}"}, payload)


def list_all(settings: Settings, path: str) -> list[dict]:
    """Every item of a paged GET. ``path`` carries its own query string."""
    items: list[dict] = []
    page = "1"
    while page:
        found, headers = api(settings, "GET", f"{path}&per_page=100&page={page}")
        items.extend(found)
        page = headers.get("X-Next-Page", "") or ""
    return items


def list_open_drafts(settings: Settings) -> list[dict]:
    return list_all(settings,
                    f"/projects/{settings.project_id}/merge_requests?state=opened&wip=yes")


def bot_user_id(settings: Settings) -> int:
    """The id of the user behind GITLAB_TOKEN, whose activity never counts."""
    user, _ = api(settings, "GET", "/user")
    return user["id"]


def person_activity(settings: Settings, mr: dict, bot_id: int) -> date:
    base = f"/projects/{settings.project_id}/merge_requests/{mr['iid']}"
    commits = list_all(settings, f"{base}/commits?")
    notes = list_all(settings, f"{base}/notes?")
    return last_person_activity(mr, commits, notes, bot_id)


def close_mr(settings: Settings, iid: int) -> None:
    base = f"/projects/{settings.project_id}/merge_requests/{iid}"
    api(settings, "POST", f"{base}/notes", {"body": CLOSE_COMMENT})
    api(settings, "PUT", base, {"state_event": "close"})


def slack_text(project_path: str, closed: list[dict]) -> str:
    lines = [f"Closed {len(closed)} stale Draft MR(s) in {project_path} "
             f"(nobody touched them for more than {STALE_BUSINESS_DAYS} business days):"]
    for mr in closed:
        lines.append(f"• !{mr['iid']} {mr['title']} {mr['web_url']}")
    return "\n".join(lines)


def post_slack(settings: Settings, text: str) -> None:
    if not settings.webhook:
        print("SLACK_WEBHOOK_URL is not set. The message would have been:")
        print(text)
        return
    http("POST", settings.webhook, {}, {"text": text})


def dry_run_wanted(env) -> bool:
    if env.get("CI_PIPELINE_SOURCE") == "merge_request_event":
        return True
    return env.get("DRY_RUN", "true").strip().lower() != "false"


def settings_from_env(env) -> Settings | None:
    missing = [name for name in ("GITLAB_TOKEN", "CI_PROJECT_ID") if not env.get(name)]
    if missing:
        print(f"set {', '.join(missing)} before running this script")
        return None
    return Settings(
        api_url=env.get("CI_API_V4_URL", DEFAULT_API).rstrip("/"),
        project_id=env["CI_PROJECT_ID"],
        project_path=env.get("CI_PROJECT_PATH", env["CI_PROJECT_ID"]),
        token=env["GITLAB_TOKEN"],
        webhook=env.get("SLACK_WEBHOOK_URL", ""),
        dry_run=dry_run_wanted(env),
    )


def main() -> int:
    settings = settings_from_env(os.environ)
    if settings is None:
        return 1
    today = datetime.now(timezone.utc).date()
    bot_id = bot_user_id(settings)
    drafts = list_open_drafts(settings)
    print(f"{len(drafts)} open Draft MR(s) in {settings.project_path}")
    stale = []
    for mr in drafts:
        last_active = person_activity(settings, mr, bot_id)
        age = business_days_since(last_active, today)
        flag = "ok"
        if is_stale(mr, last_active, today):
            stale.append(mr)
            flag = "STALE"
        print(f"  {flag:5} !{mr['iid']} {age} business day(s) since a person "
              f"touched it  {mr['title']}")

    if not stale:
        print("nothing to close")
        return 0
    if settings.dry_run:
        print(f"dry run: {len(stale)} MR(s) would be closed, nothing changed "
              "(set DRY_RUN=false on a scheduled or web pipeline to arm)")
        return 0

    for mr in stale:
        close_mr(settings, mr["iid"])
        print(f"closed !{mr['iid']} {mr['title']}")
    post_slack(settings, slack_text(settings.project_path, stale))
    print(f"closed {len(stale)} stale Draft MR(s)")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except HttpFailure as failure:
        print(failure)
        sys.exit(1)
