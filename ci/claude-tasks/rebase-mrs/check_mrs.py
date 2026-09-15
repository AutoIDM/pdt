"""Judge the rebase Claude just did and tell the MR author what happened.

This is the ``check`` hook of the rebase-mrs task. It reads
``{"item", "result", "job_url"}`` on stdin and prints a verdict:

  ok            the branch now sits on the default branch with no more
                commits than before. No comment unless conflicts were
                resolved, in which case the author is asked to review.
  needs_human   nothing was pushed, what was pushed is not a plain rebase, or
                the branch could not be read at all.
                The MR gets a comment with the job link and, when the branch
                changed, the commit to restore.

Claude has already done the work by the time this runs, so nothing here ends
the job as an error. A branch that has vanished because the MR was merged
while Claude rebased it is a normal race and counts as ok; a fetch that fails
for any other reason is retried, then handed to a person.

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
import time
import urllib.request

TIMEOUT = 60
DEFAULT_API = "https://gitlab.com/api/v4"
FETCH_ATTEMPTS = 3
FETCH_PAUSE_SECONDS = 5
GONE_STATES = ("merged", "closed")
# What git says when origin has no such branch, as opposed to not answering.
MISSING_REF_SIGNS = ("couldn't find remote ref", "could not find remote ref")


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


def comment_for(status: str, message: str, result_text: str, job_url: str,
                checked: bool = True) -> str:
    """The MR note to post, or "" for a quiet success.

    ``checked`` False means the branch was never compared, so the note must
    not claim a rebase happened.
    """
    link = f" Job log: {job_url}" if job_url else ""
    if status == "ok":
        if not checked or not mentions_conflicts(result_text):
            return ""
        return ("Rebased onto the default branch by pdt CI. Claude Code resolved conflicts, "
                f"so please review the result.{link}\n\nClaude's summary:\n\n{result_text}")
    return (f"pdt CI could not rebase this merge request: {message}.{link}\n\n"
            f"Claude's summary:\n\n{result_text or '(none)'}")


def is_missing_ref(problem: str) -> bool:
    """True when git says origin has no such branch, not that origin did not answer."""
    lower = (problem or "").lower()
    return any(sign in lower for sign in MISSING_REF_SIGNS)


def fetch_verdict(branch: str, problem: str, state: str) -> tuple[str, str]:
    """Pure verdict when origin/<branch> could not be fetched.

    ``state`` is the merge request's state, or "" when GitLab could not be
    asked. A branch that is gone from a merged or closed MR is the race that
    happens when someone merges while Claude rebases, and needs nobody.
    """
    if not is_missing_ref(problem):
        return "needs_human", (f"could not read origin/{branch} after {FETCH_ATTEMPTS} tries, so "
                               f"Claude's push was not checked: {problem}")
    if state in GONE_STATES:
        return "ok", (f"the merge request was {state} while the rebase ran, so origin/{branch} "
                      f"is gone and there is nothing left to check")
    return "needs_human", (f"origin has no branch {branch} any more, but the merge request is "
                           f"still open; see whether it was renamed or deleted")


def git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout.strip()


def fetch_branch(name: str, pause=time.sleep) -> str:
    """Fetch one branch from origin. Returns "" or git's last complaint.

    A fetch fails both when origin is briefly unreachable, which another try
    usually cures, and when the branch is gone, which no number of tries will.
    """
    problem = ""
    for attempt in range(1, FETCH_ATTEMPTS + 1):
        done = subprocess.run(
            ["git", "fetch", "--quiet", "origin", f"+refs/heads/{name}:refs/remotes/origin/{name}"],
            capture_output=True, text=True, check=False)
        if done.returncode == 0:
            return ""
        problem = (done.stderr or done.stdout).strip() or f"git fetch exited {done.returncode}"
        if is_missing_ref(problem) or attempt == FETCH_ATTEMPTS:
            return problem
        pause(FETCH_PAUSE_SECONDS * attempt)
    return problem


def mr_state(env, iid: int) -> str:
    """The merge request's state, or "" when GitLab cannot be asked."""
    if not env.get("GITLAB_TOKEN") or not env.get("CI_PROJECT_ID"):
        return ""
    api_url = env.get("CI_API_V4_URL", DEFAULT_API).rstrip("/")
    request = urllib.request.Request(
        f"{api_url}/projects/{env['CI_PROJECT_ID']}/merge_requests/{iid}",
        headers={"PRIVATE-TOKEN": env["GITLAB_TOKEN"]})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return str(json.loads(response.read()).get("state", ""))
    except (OSError, ValueError):
        return ""


def post_note(env, iid: int, body: str) -> None:
    api_url = env.get("CI_API_V4_URL", DEFAULT_API).rstrip("/")
    request = urllib.request.Request(
        f"{api_url}/projects/{env['CI_PROJECT_ID']}/merge_requests/{iid}/notes",
        method="POST", data=json.dumps({"body": body}).encode(),
        headers={"PRIVATE-TOKEN": env["GITLAB_TOKEN"], "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=TIMEOUT):
        pass


def verdict(env, item: dict, target: str, branch: str) -> tuple[str, str, bool]:
    """Judge what origin holds now: (status, message, branch was compared).

    It reports a problem instead of raising, so a git that cannot reach
    origin does not fail a job in which Claude already did the work.
    """
    problem = fetch_branch(target)
    if problem:
        return "needs_human", (f"could not read origin/{target} after {FETCH_ATTEMPTS} tries, so "
                               f"Claude's push was not checked: {problem}"), False
    problem = fetch_branch(branch)
    if problem:
        return (*fetch_verdict(branch, problem, mr_state(env, int(item["iid"]))), False)
    try:
        after_sha = git("rev-parse", f"origin/{branch}")
        default_sha = git("rev-parse", f"origin/{target}")
        merge_base = git("merge-base", f"origin/{target}", f"origin/{branch}")
        after_count = int(git("rev-list", "--count", f"origin/{target}..origin/{branch}"))
    except (subprocess.CalledProcessError, ValueError) as trouble:
        return "needs_human", (f"could not compare origin/{branch} with origin/{target}, so "
                               f"Claude's push was not checked: {trouble}"), False
    return (*classify(item["before_sha"], after_sha, default_sha, merge_base,
                      int(item["ahead"]), after_count), True)


def main() -> int:
    payload = json.load(sys.stdin)
    item, result, job_url = payload["item"], payload["result"], payload.get("job_url", "")
    env = os.environ
    target = item.get("target_branch") or env.get("CI_DEFAULT_BRANCH", "master")
    branch = item["source_branch"]

    status, message, checked = verdict(env, item, target, branch)

    note = comment_for(status, message, str(result.get("result", "")), job_url, checked)
    if note and env.get("GITLAB_TOKEN") and env.get("CI_PROJECT_ID"):
        post_note(env, int(item["iid"]), note)
        print("posted a comment on the merge request", file=sys.stderr)
    print(json.dumps({"status": status, "message": message}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
