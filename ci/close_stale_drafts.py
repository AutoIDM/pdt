"""Close Draft merge requests that nobody has touched for a while.

A scheduled GitLab pipeline runs this once a day. It lists the project's open
Draft merge requests, and any one whose last activity is more than
STALE_BUSINESS_DAYS business days old gets the comment CLOSE_COMMENT, is
closed, and is listed in one message to Slack.

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


def updated_on(mr: dict) -> date:
    return datetime.fromisoformat(mr["updated_at"]).astimezone(timezone.utc).date()


def is_stale(mr: dict, today: date) -> bool:
    return bool(mr.get("draft")) and (
        business_days_since(updated_on(mr), today) > STALE_BUSINESS_DAYS)


def stale_drafts(mrs: list[dict], today: date) -> list[dict]:
    return [mr for mr in mrs if is_stale(mr, today)]


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


def list_open_drafts(settings: Settings) -> list[dict]:
    mrs: list[dict] = []
    page = "1"
    while page:
        found, headers = api(
            settings, "GET",
            f"/projects/{settings.project_id}/merge_requests"
            f"?state=opened&wip=yes&per_page=100&page={page}")
        mrs.extend(found)
        page = headers.get("X-Next-Page", "") or ""
    return mrs


def close_mr(settings: Settings, iid: int) -> None:
    base = f"/projects/{settings.project_id}/merge_requests/{iid}"
    api(settings, "POST", f"{base}/notes", {"body": CLOSE_COMMENT})
    api(settings, "PUT", base, {"state_event": "close"})


def slack_text(project_path: str, closed: list[dict]) -> str:
    lines = [f"Closed {len(closed)} stale Draft MR(s) in {project_path} "
             f"(no activity for more than {STALE_BUSINESS_DAYS} business days):"]
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
    drafts = list_open_drafts(settings)
    print(f"{len(drafts)} open Draft MR(s) in {settings.project_path}")
    for mr in drafts:
        age = business_days_since(updated_on(mr), today)
        flag = "STALE" if is_stale(mr, today) else "ok"
        print(f"  {flag:5} !{mr['iid']} {age} business day(s) idle  {mr['title']}")

    stale = stale_drafts(drafts, today)
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
