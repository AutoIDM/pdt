"""Judge the rebase Claude just did and tell the MR author what happened.

This is the ``check`` hook of the rebase-mrs task. It reads
``{"item", "result", "job_url"}`` on stdin and prints a verdict:

  ok            the branch now sits on the default branch with no more
                commits than before. No comment unless conflicts were
                resolved, in which case the author is asked to review.
  needs_human   nothing was pushed, what was pushed is not a plain rebase, or
                the comment the author needed never posted.
                The MR gets a comment with the job link and, when the branch
                changed, the commit to restore.

A comment that will not post never fails the job. GitLab refusing the note
says something about the token, not about the rebase, so the reason goes in
the job log and the verdict becomes needs_human at worst.

It runs in the item's own worktree, which the runner deletes afterwards,
so it leaves the checkout as it finds it. Stdlib only.

Environment:
  GITLAB_TOKEN, CI_API_V4_URL, CI_PROJECT_ID, CI_DEFAULT_BRANCH   as in select_mrs.py.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

TIMEOUT = 60
DEFAULT_API = "https://gitlab.com/api/v4"
TOKEN_FIX = ("give GITLAB_TOKEN the api scope (read_api lists merge requests but cannot "
             "comment) and the Reporter role or higher on the project; see the handbook, "
             "developer.md, \"Claude Code in CI\"")


def classify(before_sha: str, after_sha: str, default_sha: str, merge_base: str,
             ahead: int, after_count: int) -> tuple[str, str]:
    """Pure verdict on one branch. Returns (status, message)."""
    if after_sha == before_sha:
        return "needs_human", "Claude did not push; the branch is unchanged"
    if merge_base != default_sha:
        return "needs_human", (f"the branch was pushed but does not sit on the default branch; "
                               f"restore it with: git push --force-with-lease origin {before_sha}:")
    if after_count < 1 or after_count > ahead:
        return "needs_human", (f"the branch was pushed with {after_count} commit(s) where "
                               f"{ahead} were expected; restore it with: "
                               f"git push --force-with-lease origin {before_sha}:")
    return "ok", f"rebased onto the default branch ({after_count} commit(s))"


def mentions_conflicts(text: str) -> bool:
    lower = (text or "").lower()
    return "conflict" in lower and "no conflicts" not in lower


def comment_for(status: str, message: str, result_text: str, job_url: str) -> str:
    """The MR note to post, or "" for a quiet success."""
    link = f" Job log: {job_url}" if job_url else ""
    if status == "ok":
        if not mentions_conflicts(result_text):
            return ""
        return ("Rebased onto the default branch by pdt CI. Claude Code resolved conflicts, "
                f"so please review the result.{link}\n\nClaude's summary:\n\n{result_text}")
    return (f"pdt CI could not rebase this merge request: {message}.{link}\n\n"
            f"Claude's summary:\n\n{result_text or '(none)'}")


def note_problem(code: int | None, detail: str) -> str:
    """Why the comment did not post, naming what a person must change."""
    if code in (401, 403):
        return f"GitLab refused the comment (HTTP {code}); {TOKEN_FIX}"
    if code is None:
        return f"the comment could not be sent to GitLab: {detail}"
    return f"GitLab refused the comment (HTTP {code}): {detail}"


def with_note_problem(status: str, message: str, problem: str) -> tuple[str, str]:
    """Fold a comment that did not post into the verdict. A rebase stays rebased."""
    if not problem:
        return status, message
    if status == "ok":
        return "needs_human", f"{message}, but the author was not told: {problem}"
    return status, f"{message}; the author was not told either: {problem}"


def git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout.strip()


def post_note(env, iid: int, body: str) -> str:
    """Comment on the merge request. Returns "" or why the comment did not post."""
    api_url = env.get("CI_API_V4_URL", DEFAULT_API).rstrip("/")
    request = urllib.request.Request(
        f"{api_url}/projects/{env['CI_PROJECT_ID']}/merge_requests/{iid}/notes",
        method="POST", data=json.dumps({"body": body}).encode(),
        headers={"PRIVATE-TOKEN": env["GITLAB_TOKEN"], "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT):
            return ""
    except urllib.error.HTTPError as refused:
        return note_problem(refused.code, str(refused.reason))
    except OSError as unreachable:
        return note_problem(None, str(unreachable))


def main() -> int:
    payload = json.load(sys.stdin)
    item, result, job_url = payload["item"], payload["result"], payload.get("job_url", "")
    env = os.environ
    target = item.get("target_branch") or env.get("CI_DEFAULT_BRANCH", "master")
    branch = item["source_branch"]

    git("fetch", "--quiet", "origin",
        f"+refs/heads/{branch}:refs/remotes/origin/{branch}",
        f"+refs/heads/{target}:refs/remotes/origin/{target}")
    after_sha = git("rev-parse", f"origin/{branch}")
    default_sha = git("rev-parse", f"origin/{target}")
    merge_base = git("merge-base", f"origin/{target}", f"origin/{branch}")
    after_count = int(git("rev-list", "--count", f"origin/{target}..origin/{branch}"))
    status, message = classify(item["before_sha"], after_sha, default_sha, merge_base,
                               int(item["ahead"]), after_count)

    note = comment_for(status, message, str(result.get("result", "")), job_url)
    if note and env.get("GITLAB_TOKEN") and env.get("CI_PROJECT_ID"):
        problem = post_note(env, int(item["iid"]), note)
        print(problem or "posted a comment on the merge request", file=sys.stderr)
        status, message = with_note_problem(status, message, problem)
    print(json.dumps({"status": status, "message": message}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
