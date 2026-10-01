"""Close Draft pull requests that nobody has touched for a while.

A scheduled GitHub Actions run does this once a day. It lists the
repository's open Draft pull requests, and any one that no person has touched
for more than STALE_BUSINESS_DAYS business days gets the comment
CLOSE_COMMENT, is closed, and is listed in one message to Slack.

"Touched by a person" means the PR was opened, a commit was authored, or a
comment or review was written by anyone other than the user behind GH_TOKEN
or a bot (a login ending in ``[bot]``). So a label or comment from
``score-prs``, or any other automated change, does not keep a Draft alive. A
rebase keeps each commit's author date, so pushed-again commits do not count
either. The PR's ``updated_at`` is not used, because GitHub bumps it for
those changes too.

Environment:
  GH_TOKEN            token that may comment on and close pull requests.
  SLACK_WEBHOOK_URL   Slack incoming webhook. When unset the message is
                      printed instead of posted.
  DRY_RUN             anything but the exact word ``false`` means list the
                      candidates and change nothing. The workflow sets it to
                      false on its schedule.
  GITHUB_REPOSITORY, GITHUB_API_URL   set by GitHub Actions.

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
CLOSE_COMMENT = "Stale Draft PR, closing"
TIMEOUT = 60
DEFAULT_API = "https://api.github.com"
# What GET /user cannot tell for GITHUB_TOKEN: the login its comments carry.
ACTIONS_BOT = "github-actions[bot]"


class HttpFailure(Exception):
    """An HTTP call answered with an error status."""

    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


@dataclass
class Settings:
    api_url: str
    repo: str
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


def is_person(user: dict | None, own_login: str) -> bool:
    login = (user or {}).get("login", "")
    return bool(login) and login != own_login and not login.endswith("[bot]")


def last_person_activity(pr: dict, commits: list[dict], comments: list[dict],
                         reviews: list[dict], own_login: str) -> date:
    """The latest date a person touched the PR.

    Counts the PR being opened, each commit's author date (a rebase rewrites
    the committer date but keeps the author date), and each comment and
    submitted review whose author is a person (see ``is_person``).
    """
    stamps = [pr["created_at"]]
    stamps += [commit["commit"]["author"]["date"] for commit in commits]
    stamps += [comment["updated_at"] for comment in comments
               if is_person(comment.get("user"), own_login)]
    stamps += [review["submitted_at"] for review in reviews
               if review.get("submitted_at") and is_person(review.get("user"), own_login)]
    return max(utc_date(stamp) for stamp in stamps)


def is_stale(pr: dict, last_active: date, today: date) -> bool:
    return bool(pr.get("draft")) and (
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
        raise HttpFailure(error.code, f"{method} {url} returned {error.code}: {detail}") from None
    parsed = json.loads(body) if body.startswith((b"{", b"[")) else body.decode()
    return parsed, response.headers


def api(settings: Settings, method: str, path: str, payload: dict | None = None):
    return http(method, f"{settings.api_url}{path}", auth(settings), payload)


def auth(settings: Settings) -> dict:
    return {"Authorization": f"Bearer {settings.token}",
            "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}


def next_link(header: str) -> str:
    for part in (header or "").split(","):
        url, _, rel = part.partition(";")
        if 'rel="next"' in rel:
            return url.strip(" <>")
    return ""


def list_all(settings: Settings, path: str) -> list[dict]:
    """Every item of a paged GET. ``path`` carries its own query string."""
    items: list[dict] = []
    url = f"{settings.api_url}{path}&per_page=100"
    while url:
        found, headers = http("GET", url, auth(settings))
        items.extend(found)
        url = next_link(headers.get("Link", ""))
    return items


def list_open_drafts(settings: Settings) -> list[dict]:
    pulls = list_all(settings, f"/repos/{settings.repo}/pulls?state=open")
    return [pr for pr in pulls if pr.get("draft")]


def own_login(settings: Settings) -> str:
    """The login behind GH_TOKEN, whose activity never counts."""
    try:
        user, _ = api(settings, "GET", "/user")
    except HttpFailure as failure:
        if failure.code != 403:
            raise
        return ACTIONS_BOT
    return user["login"]


def person_activity(settings: Settings, pr: dict, login: str) -> date:
    number = pr["number"]
    commits = list_all(settings, f"/repos/{settings.repo}/pulls/{number}/commits?")
    comments = list_all(settings, f"/repos/{settings.repo}/issues/{number}/comments?")
    reviews = list_all(settings, f"/repos/{settings.repo}/pulls/{number}/reviews?")
    return last_person_activity(pr, commits, comments, reviews, login)


def close_pr(settings: Settings, number: int) -> None:
    api(settings, "POST", f"/repos/{settings.repo}/issues/{number}/comments",
        {"body": CLOSE_COMMENT})
    api(settings, "PATCH", f"/repos/{settings.repo}/pulls/{number}", {"state": "closed"})


def slack_text(repo: str, closed: list[dict]) -> str:
    lines = [f"Closed {len(closed)} stale Draft PR(s) in {repo} "
             f"(nobody touched them for more than {STALE_BUSINESS_DAYS} business days):"]
    for pr in closed:
        lines.append(f"• #{pr['number']} {pr['title']} {pr['html_url']}")
    return "\n".join(lines)


def post_slack(settings: Settings, text: str) -> None:
    if not settings.webhook:
        print("SLACK_WEBHOOK_URL is not set. The message would have been:")
        print(text)
        return
    http("POST", settings.webhook, {}, {"text": text})


def dry_run_wanted(env) -> bool:
    return env.get("DRY_RUN", "true").strip().lower() != "false"


def settings_from_env(env) -> Settings | None:
    missing = [name for name in ("GH_TOKEN", "GITHUB_REPOSITORY") if not env.get(name)]
    if missing:
        print(f"set {', '.join(missing)} before running this script")
        return None
    return Settings(
        api_url=env.get("GITHUB_API_URL", DEFAULT_API).rstrip("/"),
        repo=env["GITHUB_REPOSITORY"],
        token=env["GH_TOKEN"],
        webhook=env.get("SLACK_WEBHOOK_URL", ""),
        dry_run=dry_run_wanted(env),
    )


def main() -> int:
    settings = settings_from_env(os.environ)
    if settings is None:
        return 1
    today = datetime.now(timezone.utc).date()
    login = own_login(settings)
    drafts = list_open_drafts(settings)
    print(f"{len(drafts)} open Draft PR(s) in {settings.repo}")
    stale = []
    for pr in drafts:
        last_active = person_activity(settings, pr, login)
        age = business_days_since(last_active, today)
        flag = "ok"
        if is_stale(pr, last_active, today):
            stale.append(pr)
            flag = "STALE"
        print(f"  {flag:5} #{pr['number']} {age} business day(s) since a person "
              f"touched it  {pr['title']}")

    if not stale:
        print("nothing to close")
        return 0
    if settings.dry_run:
        print(f"dry run: {len(stale)} PR(s) would be closed, nothing changed "
              "(clear the dry_run box on a manual run to arm)")
        return 0

    for pr in stale:
        close_pr(settings, pr["number"])
        print(f"closed #{pr['number']} {pr['title']}")
    post_slack(settings, slack_text(settings.repo, stale))
    print(f"closed {len(stale)} stale Draft PR(s)")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except HttpFailure as failure:
        print(failure)
        sys.exit(1)
