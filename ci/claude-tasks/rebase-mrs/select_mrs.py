"""Rebase the open merge requests that git can rebase alone; list the rest.

This is the ``select`` hook of the rebase-mrs task. It prints a JSON list to
stdout, one item per MR that needs Claude, and progress lines to stderr.
Each item is one Claude session.

An MR is skipped when it comes from a fork, or carries the label
``no-autorebase``. Drafts are included. An MR whose branch already contains
the default branch's tip is up to date and is not listed.

A stale MR is first rebased with plain git in a throwaway worktree. When
that finishes with no conflict and the same number of commits, the branch
is pushed with ``--force-with-lease`` and Claude never sees it; the MR's own
pipeline tests the result. Only an MR whose rebase stops on a conflict
becomes an item. Most rebases are clean, and a Claude session for a clean
rebase costs about a minute and half a dollar for what git does in a second.

It also points ``origin`` at an HTTPS URL that carries GITLAB_TOKEN and sets
the bot's git identity, so it and Claude can fetch and push.

Environment:
  GITLAB_TOKEN   project access token with ``api`` and ``write_repository``.
  CI_API_V4_URL, CI_PROJECT_ID, CI_SERVER_HOST, CI_PROJECT_PATH,
  CI_DEFAULT_BRANCH   set by GitLab CI.
  CLAUDE_TASK_DRY_RUN   set by the runner's --dry-run: report which MRs git
                 would rebase, push nothing, and still list the conflicts.

Stdlib only.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
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


def keeps_commits(after: int, ahead: int) -> bool:
    """A rebase is only clean when the branch still has its commits.

    git drops a commit whose change is already on the new base, so fewer is
    fine; none at all means the MR is already merged in substance, and more
    means something went wrong. Both go to Claude, who reports it.
    """
    return 1 <= after <= ahead


def rebase_with_git(target: str, branch: str, before_sha: str, ahead: int, push: bool) -> bool:
    """Try the rebase in a throwaway worktree. True when git did it alone.

    ``push`` False (a dry run) still says whether git could have done it.
    A push that fails (the branch moved, or the token cannot write) also
    returns False so Claude retries and the check hook reports.
    """
    with tempfile.TemporaryDirectory(prefix="pdt-rebase-") as tmp:
        path = os.path.join(tmp, "repo")
        git("worktree", "add", "--quiet", "--detach", path, f"origin/{branch}")
        try:
            if not git_ok("-C", path, "rebase", f"origin/{target}"):
                git_ok("-C", path, "rebase", "--abort")
                return False
            after = int(git("-C", path, "rev-list", "--count", f"origin/{target}..HEAD"))
            if not keeps_commits(after, ahead):
                return False
            if push:
                return git_ok("-C", path, "push", "--quiet",
                              f"--force-with-lease=refs/heads/{branch}:{before_sha}",
                              "origin", f"HEAD:refs/heads/{branch}")
            return True
        finally:
            git_ok("worktree", "remove", "--force", path)


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
    dry_run = bool(env.get("CLAUDE_TASK_DRY_RUN"))

    git("remote", "set-url", "origin",
        f"https://oauth2:{token}@{env['CI_SERVER_HOST']}/{env['CI_PROJECT_PATH']}.git")
    git("config", "user.name", BOT_NAME)
    git("config", "user.email", BOT_EMAIL)

    mrs = [mr for mr in list_open_mrs(api_url, token, project_id, target) if wanted(mr, project_id)]
    log(f"{len(mrs)} open MR(s) targeting {target} qualify"
        + (" (dry run: nothing is pushed)" if dry_run else ""))
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
        if rebase_with_git(target, branch, before_sha, ahead, push=not dry_run):
            word = "would rebase" if dry_run else "rebased    "
            log(f"  {word} !{mr['iid']} {mr['title']} (git alone, {ahead} commit(s))")
            continue
        log(f"  conflicts   !{mr['iid']} {mr['title']} ({ahead} commit(s) ahead)")
        items.append(item_for(mr, before_sha, ahead))
    print(json.dumps(items))
    return 0


if __name__ == "__main__":
    sys.exit(main())
