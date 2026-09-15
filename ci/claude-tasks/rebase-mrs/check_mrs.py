"""Judge the rebase Claude just did and tell the MR author what happened.

This is the ``check`` hook of the rebase-mrs task. It reads
``{"item", "result", "job_url"}`` on stdin and prints a verdict:

  ok            the branch now sits on the default branch with no more
                commits than before. No comment unless conflicts were
                resolved, in which case the author is asked to review.
  needs_human   nothing was pushed, or what was pushed is not a plain rebase.
                The MR gets a comment with the job link and, when the branch
                changed, the commit to restore.

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
import urllib.request

TIMEOUT = 60
DEFAULT_API = "https://gitlab.com/api/v4"


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


def git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout.strip()


def post_note(env, iid: int, body: str) -> None:
    api_url = env.get("CI_API_V4_URL", DEFAULT_API).rstrip("/")
    request = urllib.request.Request(
        f"{api_url}/projects/{env['CI_PROJECT_ID']}/merge_requests/{iid}/notes",
        method="POST", data=json.dumps({"body": body}).encode(),
        headers={"PRIVATE-TOKEN": env["GITLAB_TOKEN"], "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=TIMEOUT):
        pass


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
        post_note(env, int(item["iid"]), note)
        print("posted a comment on the merge request", file=sys.stderr)
    print(json.dumps({"status": status, "message": message}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
