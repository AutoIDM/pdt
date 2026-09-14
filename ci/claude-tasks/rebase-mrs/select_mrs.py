"""List the open merge requests that are not rebased onto the default branch.

This is the ``select`` hook of the rebase-mrs task. It prints a JSON list to
stdout, one item per stale MR, and progress lines to stderr. Each item is
one Claude session.

An MR is skipped when it comes from a fork, or carries the label
``no-autorebase``. Drafts are included. An MR whose branch already contains
the default branch's tip is up to date and is not listed.

It also points ``origin`` at an HTTPS URL that carries GITLAB_TOKEN and sets
the bot's git identity, so Claude can fetch and push in the same checkout.

Environment:
  GITLAB_TOKEN   project access token with ``api`` and ``write_repository``.
  CI_API_V4_URL, CI_PROJECT_ID, CI_SERVER_HOST, CI_PROJECT_PATH,
  CI_DEFAULT_BRANCH   set by GitLab CI.

Stdlib only.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.request

SKIP_LABEL = "no-autorebase"
BOT_NAME = "pdt-ci-bot"
BOT_EMAIL = "pdt-ci-bot@users.noreply.gitlab.com"
TIMEOUT = 60
DEFAULT_API = "https://gitlab.com/api/v4"
REQUIRED = ("GITLAB_TOKEN", "CI_PROJECT_ID", "CI_SERVER_HOST", "CI_PROJECT_PATH")


def log(text: str) -> None:
    print(text, file=sys.stderr)


def wanted(mr: dict, project_id) -> bool:
    """Keep same-project MRs without the opt-out label. Drafts stay in."""
    if str(mr.get("source_project_id")) != str(project_id):
        return False
    if SKIP_LABEL in (mr.get("labels") or []):
        return False
    return True


def item_for(mr: dict, before_sha: str, ahead: int) -> dict:
    return {
        "id": mr["iid"],
        "iid": mr["iid"],
        "title": mr["title"],
        "source_branch": mr["source_branch"],
        "target_branch": mr["target_branch"],
        "before_sha": before_sha,
        "ahead": ahead,
    }


def api_get(api_url: str, token: str, path: str):
    request = urllib.request.Request(f"{api_url}{path}", headers={"PRIVATE-TOKEN": token})
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return json.loads(response.read()), response.headers


def list_open_mrs(api_url: str, token: str, project_id: str, target: str) -> list[dict]:
    mrs: list[dict] = []
    page = "1"
    while page:
        found, headers = api_get(
            api_url, token,
            f"/projects/{project_id}/merge_requests?state=opened"
            f"&target_branch={target}&per_page=100&page={page}")
        mrs.extend(found)
        page = headers.get("X-Next-Page", "") or ""
    return mrs


def git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout.strip()


def git_ok(*args: str) -> bool:
    return subprocess.run(["git", *args], capture_output=True, text=True, check=False).returncode == 0


def is_rebased(target: str, branch: str) -> bool:
    return git_ok("merge-base", "--is-ancestor", f"origin/{target}", f"origin/{branch}")


def main() -> int:
    env = os.environ
    missing = [name for name in REQUIRED if not env.get(name)]
    if missing:
        log(f"set {', '.join(missing)} before running the rebase-mrs task")
        return 1
    api_url = env.get("CI_API_V4_URL", DEFAULT_API).rstrip("/")
    token = env["GITLAB_TOKEN"]
    project_id = env["CI_PROJECT_ID"]
    target = env.get("CI_DEFAULT_BRANCH", "master")

    git("remote", "set-url", "origin",
        f"https://oauth2:{token}@{env['CI_SERVER_HOST']}/{env['CI_PROJECT_PATH']}.git")
    git("config", "user.name", BOT_NAME)
    git("config", "user.email", BOT_EMAIL)

    mrs = [mr for mr in list_open_mrs(api_url, token, project_id, target) if wanted(mr, project_id)]
    log(f"{len(mrs)} open MR(s) targeting {target} qualify")
    if not mrs:
        print("[]")
        return 0

    refspecs = [f"+refs/heads/{name}:refs/remotes/origin/{name}"
                for name in [target, *(mr["source_branch"] for mr in mrs)]]
    git("fetch", "--quiet", "origin", *refspecs)

    items = []
    for mr in mrs:
        branch = mr["source_branch"]
        if is_rebased(target, branch):
            log(f"  up to date  !{mr['iid']} {mr['title']}")
            continue
        before_sha = git("rev-parse", f"origin/{branch}")
        ahead = int(git("rev-list", "--count", f"origin/{target}..origin/{branch}"))
        log(f"  stale       !{mr['iid']} {mr['title']} ({ahead} commit(s) ahead)")
        items.append(item_for(mr, before_sha, ahead))
    print(json.dumps(items))
    return 0


if __name__ == "__main__":
    sys.exit(main())
