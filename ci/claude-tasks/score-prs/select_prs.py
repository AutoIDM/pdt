"""Floor every open pull request and merge the simple ones already scored.

This is the ``select`` hook of the score-prs task. For each open pull request
from this repository it reads the diff from the GitHub API, sets a floor tier
from path rules, and looks for the score comment a previous run left.

  Not scored yet, or the diff changed since: the PR becomes one item, so
  Claude reads it and may raise the floor (see check_prs.py).
  Already scored for this diff: nothing for Claude. When the tier is
  ``simple`` and the PR is mergeable (every check on the head commit passed,
  no conflicts, no unresolved review threads) it is merged right here, so a
  green PR never waits for another Claude run.
  A Draft is scored once and then left alone while it stays a Draft, however
  often it changes. When it leaves Draft it is scored again only if its diff
  changed since. A Draft is never merged.

The floor is deterministic and default-deny: a path no rule names is
``review``. The three tiers, lowest first, are simple, review, architectural.

The repository is public, so a PR from a fork is skipped: it is never
labeled, never merged, and never shown to Claude. Only a score comment the
token's own user wrote counts, so a stranger who copies the marker into a
comment cannot mark a PR simple.

It also fetches every head branch into the checkout, so Claude's session
can run ``git diff origin/<base>...origin/<branch>`` without fetching.

A run the pull_request event started sets PR_NUMBER and looks at that PR
alone. A schedule or manual run sweeps every open PR, which also picks up a
changed diff.

Environment:
  GH_TOKEN         token with contents and pull-requests write. A merge made
                   with the workflow's own GITHUB_TOKEN starts no workflow on
                   the default branch, so the workflow prefers PDT_BOT_TOKEN.
  DRY_RUN          anything but the exact word ``false`` means list and change
                   nothing.
  PR_NUMBER        the PR of a pull_request event. Absent on a sweep.
  DEFAULT_BRANCH   the branch a PR must target; ``main`` when unset.
  GITHUB_REPOSITORY, GITHUB_API_URL, GITHUB_GRAPHQL_URL   set by GitHub Actions.

Stdlib only.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from fnmatch import fnmatch

TIERS = ("simple", "review", "architectural")
MARKER_PREFIX = "<!-- pdt-pr-score v1 "
MAX_SIMPLE_FILES = 5
MAX_SIMPLE_LINES = 200
TIMEOUT = 60
DEFAULT_API = "https://api.github.com"
DEFAULT_GRAPHQL = "https://api.github.com/graphql"
REQUIRED = ("GH_TOKEN", "GITHUB_REPOSITORY")
MERGE_NOTE = ("Auto-merged as `tier::simple` by pdt CI: the rules and Claude both scored "
              "it simple, and every check passed with no conflicts and no open review threads.")
# The job id in .github/workflows/score-prs.yml, which GitHub uses as its check
# run name. Its own run is still in progress while it decides to merge.
OWN_CHECK = "score-prs"
PASSED = ("success", "neutral", "skipped")
# "unstable" means a check has not passed, which merge_blockers judges itself.
MERGEABLE_STATES = ("clean", "unstable", "has_hooks")
# What GET /user cannot tell for GITHUB_TOKEN: the login its comments carry.
ACTIONS_BOT = "github-actions[bot]"
THREADS_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $after: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviewThreads(first: 100, after: $after) {
        nodes { isResolved }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
"""

# Path rules, first match wins, checked in this order after the pyproject rule.
ARCHITECTURAL = (
    ("src/pdt/utils/*", "public API that deployed apps import"),
    ("src/pdt/deploy.py", "provider-neutral deploy flow"),
    ("src/pdt/deploy_common.py", "shared by every provider"),
    ("src/pdt/config.py", "config loading and validation"),
    ("src/pdt/cli.py", "the command line"),
    ("src/pdt/__init__.py", "package root"),
    (".github/workflows/*", "a GitHub workflow"),
    ("ci/*", "CI automation"),
    ("AGENTS.md", "repo rules"),
    ("CLAUDE.md", "repo rules"),
    ("pdt", "install shim"),
    ("pdt.bat", "install shim"),
)
REVIEW = (
    ("src/pdt/deploy_*.py", "a provider module"),
    ("src/pdt/scaffold.py", "scaffolding"),
    ("src/pdt/gcloud_sdk.py", "CLI download"),
    ("src/pdt/console.py", "CLI output"),
    ("scripts/*", "a repo script"),
    ("tests/conftest.py", "test fixtures"),
    ("uv.lock", "the lock file"),
    (".gitignore", "ignore rules"),
)
# A few changed lines in one of these files floor at simple when the PR also
# changes a test: the unit tests and the live verify deploys cover them.
SMALL_EDIT = ("src/pdt/deploy.py", "src/pdt/deploy_common.py", "src/pdt/config.py",
              "src/pdt/cli.py")
MAX_SMALL_EDIT_LINES = 4
SIMPLE = ("*.md", "tests/*", "ci/tests/*", "src/pdt/examples/*")
PYPROJECT_KEYS = ("requires-python", "build-backend", "[project.scripts]")
# Words in a changed line that mean a person should at least read the diff.
HARD_GATE = ("subprocess", "os.environ", "os.system", "shutil.rmtree", "rm -rf",
             "secret", "token", "password", "credential", "DRY_RUN", "eval(", "exec(",
             "__import__", "base64", "chmod", "sudo", "curl ", "wget ")


class HttpFailure(Exception):
    """An HTTP call answered with an error status."""

    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


def log(text: str) -> None:
    print(text, file=sys.stderr)


# --- pure rules ---------------------------------------------------------------

def rank(tier: str) -> int:
    return TIERS.index(tier)


def highest(*tiers: str) -> str:
    return max(tiers, key=rank)


def changed_lines(diff: str) -> list[str]:
    """The added and removed lines of a unified diff, without the +/- prefix."""
    out = []
    for line in (diff or "").splitlines():
        if line.startswith(("+++", "---")):
            continue
        if line.startswith(("+", "-")):
            out.append(line[1:])
    return out


def dependency_change(diff: str) -> bool:
    for line in changed_lines(diff):
        if re.match(r'\s*"', line) or any(key in line for key in PYPROJECT_KEYS):
            return True
    return False


def hard_gate_hits(diff: str) -> list[str]:
    hits = []
    for line in changed_lines(diff):
        lower = line.lower()
        for word in HARD_GATE:
            if word.lower() in lower and word not in hits:
                hits.append(word)
    return hits


def small_edit(path: str, diff: str) -> bool:
    return (path in SMALL_EDIT and len(changed_lines(diff)) <= MAX_SMALL_EDIT_LINES
            and not hard_gate_hits(diff))


def path_floor(path: str, new_file: bool = False, diff: str = "",
               tested: bool = False) -> tuple[str, str]:
    """The floor one file sets, and why. A simple file has no reason."""
    if path == "pyproject.toml":
        if dependency_change(diff):
            return "architectural", "pyproject.toml changes a dependency or build setting"
        return "review", "pyproject.toml changed"
    if fnmatch(path, "ci/tests/*"):
        return "simple", ""
    if tested and small_edit(path, diff):
        return "simple", ""
    for pattern, why in ARCHITECTURAL:
        if fnmatch(path, pattern):
            return "architectural", f"{path} is {why}"
    if new_file and fnmatch(path, "src/pdt/deploy_*.py"):
        return "architectural", f"{path} is a new provider or runtime module"
    for pattern, why in REVIEW:
        if fnmatch(path, pattern):
            return "review", f"{path} is {why}"
    if any(fnmatch(path, pattern) for pattern in SIMPLE):
        return "simple", ""
    return "review", f"{path} matches no rule"


def same_repo(pr: dict) -> bool:
    head = (pr.get("head") or {}).get("repo") or {}
    return head.get("full_name") == pr["base"]["repo"]["full_name"]


def rule_floor(pr: dict, files: list[dict]) -> tuple[str, list[str]]:
    """The floor tier of a pull request and the reasons at that tier."""
    found: list[tuple[str, str]] = []
    lines = 0
    tested = any(fnmatch(file["filename"], "tests/*") for file in files)
    for file in files:
        path = file["filename"]
        diff = file.get("patch") or ""
        status = file.get("status")
        lines += len(changed_lines(diff))
        found.append(path_floor(path, status == "added", diff, tested))
        if status == "removed":
            found.append(("review", f"{path} is deleted"))
        if status == "renamed":
            found.append(("review", f"{path} is renamed"))
        if "patch" not in file and file.get("changes"):
            found.append(("review", f"{path} diff is too large or binary to inspect"))
        for word in hard_gate_hits(diff):
            found.append(("review", f"{path} mentions `{word}`"))
    if len(files) > MAX_SIMPLE_FILES:
        found.append(("review", f"{len(files)} files changed, more than {MAX_SIMPLE_FILES}"))
    if lines > MAX_SIMPLE_LINES:
        found.append(("review", f"{lines} lines changed, more than {MAX_SIMPLE_LINES}"))
    if pr.get("changed_files", len(files)) > len(files):
        found.append(("review", "GitHub truncated the diff"))
    if not same_repo(pr):
        found.append(("review", "comes from a fork"))
    floor = highest("simple", *(tier for tier, _ in found))
    reasons = [why for tier, why in found if tier == floor and why]
    if floor == "simple":
        reasons = ["only documentation, tests, or example apps changed"]
        small = [file["filename"] for file in files
                 if small_edit(file["filename"], file.get("patch") or "")]
        if small:
            reasons = [f"{', '.join(small)} changes at most {MAX_SMALL_EDIT_LINES} lines "
                       "and the PR changes a test"]
    return floor, reasons


def diff_id(files: list[dict]) -> str:
    """A short id for the diff's content. A clean rebase keeps it the same."""
    digest = hashlib.sha256()
    for file in sorted(files, key=lambda f: f["filename"]):
        digest.update(f"{file['filename']}\n{file.get('patch') or ''}\n".encode())
    return digest.hexdigest()[:12]


def merge_blockers(pr: dict, checks: list[dict], statuses: list[dict],
                   unresolved: int) -> list[str]:
    """Why a simple PR cannot be merged right now. Empty means go.

    ``checks`` and ``statuses`` are the check runs and commit statuses on the
    head commit, so a result from an older commit never counts.
    """
    blockers = []
    if pr.get("draft"):
        blockers.append("still a draft")
    if pr.get("mergeable") is False or pr.get("mergeable_state") == "dirty":
        blockers.append("has conflicts")
    if unresolved:
        blockers.append("unresolved review threads")
    runs = [run for run in checks if run.get("name") != OWN_CHECK]
    if not runs and not statuses:
        blockers.append("no checks ran on the head commit")
    for run in runs:
        if run.get("status") != "completed":
            blockers.append(f"check {run.get('name')} {run.get('status')}")
        elif run.get("conclusion") not in PASSED:
            blockers.append(f"check {run.get('name')} {run.get('conclusion')}")
    for status in statuses:
        if status.get("state") != "success":
            blockers.append(f"status {status.get('context')} {status.get('state')}")
    state = pr.get("mergeable_state")
    if state not in (*MERGEABLE_STATES, "dirty", "draft"):
        blockers.append(f"merge state {state}")
    if not same_repo(pr):
        blockers.append("comes from a fork")
    return blockers


# --- the score note marker ----------------------------------------------------

def format_marker(diff: str, floor: str, tier: str, claude: str) -> str:
    return f"{MARKER_PREFIX}diff={diff} floor={floor} tier={tier} claude={claude} -->"


def parse_marker(body: str) -> dict | None:
    first = (body or "").strip().splitlines()[:1]
    if not first or not first[0].startswith(MARKER_PREFIX) or not first[0].endswith("-->"):
        return None
    fields = dict(part.split("=", 1) for part in first[0][len(MARKER_PREFIX):-3].split()
                  if "=" in part)
    if {"diff", "floor", "tier", "claude"} - set(fields):
        return None
    return fields


def previous_score(comments: list[dict], login: str) -> dict | None:
    """The newest score comment's marker. Comments arrive oldest first, and
    only those the token's own user wrote count."""
    for comment in reversed(comments):
        if (comment.get("user") or {}).get("login") != login:
            continue
        marker = parse_marker(comment.get("body", ""))
        if marker:
            return marker
    return None


def already_scored(pr: dict, previous: dict | None, did: str) -> bool:
    """Nothing for Claude: scored for this diff, or a Draft scored at any diff."""
    if previous is None or previous["claude"] == "unavailable":
        return False
    return previous["diff"] == did or bool(pr.get("draft"))


def item_for(pr: dict, did: str, floor: str, reasons: list[str], files: list[dict],
             previous: dict | None) -> dict:
    listed = []
    for file in files:
        path, status = file["filename"], file.get("status")
        if status == "renamed":
            path += f" (renamed from {file.get('previous_filename')})"
        elif status in ("added", "removed"):
            path += f" ({status})"
        listed.append(path)
    return {
        "id": pr["number"],
        "number": pr["number"],
        "title": pr["title"],
        "head_ref": pr["head"]["ref"],
        "base_ref": pr["base"]["ref"],
        "head_sha": pr["head"]["sha"],
        "diff_id": did,
        "floor": floor,
        "floor_reasons": "; ".join(reasons),
        "floor_reason_list": reasons,
        "files": "\n".join(listed),
        "labels": [label["name"] for label in pr.get("labels") or []],
        "previous": previous,
    }


# --- GitHub -------------------------------------------------------------------

def http(method: str, url: str, headers: dict, payload: dict | None = None):
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        url, method=method, data=data, headers={**headers, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            body = response.read()
            response_headers = response.headers
    except urllib.error.HTTPError as error:
        detail = error.read().decode(errors="replace")[:300]
        raise HttpFailure(error.code, f"{method} {url} returned {error.code}: {detail}") from None
    parsed = json.loads(body) if body.startswith((b"{", b"[")) else body.decode()
    return parsed, response_headers


def auth(env) -> dict:
    return {"Authorization": f"Bearer {env['GH_TOKEN']}",
            "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}


def api(env, method: str, path: str, payload: dict | None = None):
    api_url = env.get("GITHUB_API_URL", DEFAULT_API).rstrip("/")
    return http(method, f"{api_url}{path}", auth(env), payload)


def next_link(header: str) -> str:
    for part in (header or "").split(","):
        url, _, rel = part.partition(";")
        if 'rel="next"' in rel:
            return url.strip(" <>")
    return ""


def paginate(env, path: str, key: str = "") -> list:
    """GET every page of ``path``. ``key`` names the list inside an object reply."""
    items: list = []
    url = env.get("GITHUB_API_URL", DEFAULT_API).rstrip("/") + path
    while url:
        found, headers = http("GET", url, auth(env))
        items.extend(found[key] if key else found)
        url = next_link(headers.get("Link", ""))
    return items


def repo_path(env) -> str:
    return f"/repos/{env['GITHUB_REPOSITORY']}"


def token_login(env) -> str:
    try:
        return api(env, "GET", "/user")[0]["login"]
    except HttpFailure as failure:
        if failure.code != 403:
            raise
        return ACTIONS_BOT


def list_open_prs(env, base: str) -> list[dict]:
    return paginate(env, f"{repo_path(env)}/pulls?state=open&base={base}&per_page=100")


def get_pr(env, number: int) -> dict:
    """One pull request in full. The list endpoint leaves out mergeable,
    mergeable_state, and changed_files, which the merge gates and the rules read."""
    return api(env, "GET", f"{repo_path(env)}/pulls/{number}")[0]


def list_files(env, number: int) -> list[dict]:
    return paginate(env, f"{repo_path(env)}/pulls/{number}/files?per_page=100")


def list_comments(env, number: int) -> list[dict]:
    return paginate(env, f"{repo_path(env)}/issues/{number}/comments?per_page=100")


def unresolved_threads(env, number: int) -> int:
    """REST has no review thread state, so this one call is GraphQL."""
    owner, name = env["GITHUB_REPOSITORY"].split("/")
    url = env.get("GITHUB_GRAPHQL_URL", DEFAULT_GRAPHQL)
    count, after = 0, None
    while True:
        variables = {"owner": owner, "name": name, "number": number, "after": after}
        reply, _ = http("POST", url, auth(env), {"query": THREADS_QUERY, "variables": variables})
        if reply.get("errors"):
            raise HttpFailure(200, f"GraphQL review threads of #{number}: {reply['errors']}")
        threads = reply["data"]["repository"]["pullRequest"]["reviewThreads"]
        count += sum(not thread["isResolved"] for thread in threads["nodes"])
        if not threads["pageInfo"]["hasNextPage"]:
            return count
        after = threads["pageInfo"]["endCursor"]


def gates(env, pr: dict) -> tuple[list[dict], list[dict], int]:
    """What merge_blockers reads besides the PR: checks, statuses, threads."""
    sha = pr["head"]["sha"]
    checks = paginate(env, f"{repo_path(env)}/commits/{sha}/check-runs?per_page=100",
                      "check_runs")
    statuses = api(env, "GET", f"{repo_path(env)}/commits/{sha}/status")[0]["statuses"]
    return checks, statuses, unresolved_threads(env, pr["number"])


def merge_pr(env, pr: dict) -> None:
    number = pr["number"]
    api(env, "PUT", f"{repo_path(env)}/pulls/{number}/merge",
        {"sha": pr["head"]["sha"], "merge_method": "merge"})
    api(env, "POST", f"{repo_path(env)}/issues/{number}/comments", {"body": MERGE_NOTE})
    try:
        api(env, "DELETE", f"{repo_path(env)}/git/refs/heads/{pr['head']['ref']}")
    except HttpFailure as failure:
        log(f"  branch {pr['head']['ref']} not deleted: {failure}")


def fetch_branches(branches: list[str]) -> None:
    refspecs = [f"+refs/heads/{name}:refs/remotes/origin/{name}" for name in branches]
    subprocess.run(["git", "fetch", "--quiet", "origin", *refspecs], check=True)


# --- orchestration ------------------------------------------------------------

def main() -> int:
    env = os.environ
    missing = [name for name in REQUIRED if not env.get(name)]
    if missing:
        log(f"set {', '.join(missing)} before running the score-prs task")
        return 1
    dry_run = env.get("DRY_RUN", "true").strip().lower() != "false"
    base = env.get("DEFAULT_BRANCH") or "main"
    login = token_login(env)

    prs = list_open_prs(env, base)
    event_number = env.get("PR_NUMBER", "").strip()
    if event_number:
        prs = [pr for pr in prs if str(pr["number"]) == event_number]
        log(f"pull_request event for #{event_number}: looking at that PR only"
            + ("" if prs else " (it is not an open PR of this repository)"))
    for pr in prs:
        if not same_repo(pr):
            log(f"  fork        #{pr['number']} {pr['title']} (skipped: comes from a fork)")
    prs = [pr for pr in prs if same_repo(pr)]
    log(f"{len(prs)} open PR(s) targeting {base} qualify"
        + (" (dry run: nothing is merged)" if dry_run else ""))
    if prs:
        fetch_branches([base, *(pr["head"]["ref"] for pr in prs)])

    items = []
    for listed in prs:
        pr = get_pr(env, listed["number"])
        number, title = pr["number"], pr["title"]
        files = list_files(env, number)
        did = diff_id(files)
        floor, reasons = rule_floor(pr, files)
        previous = previous_score(list_comments(env, number), login)
        if not already_scored(pr, previous, did):
            log(f"  score       #{number} {title} (floor {floor}: {'; '.join(reasons)})")
            items.append(item_for(pr, did, floor, reasons, files, previous))
            continue
        tier = previous["tier"]
        if pr.get("draft"):
            log(f"  draft       #{number} {title} ({tier}, scored once; not merged)")
            continue
        if tier != "simple":
            log(f"  {tier:11} #{number} {title} (already scored)")
            continue
        blockers = merge_blockers(pr, *gates(env, pr))
        if blockers:
            log(f"  blocked     #{number} {title} (simple, but {', '.join(blockers)})")
        elif dry_run:
            log(f"  would merge #{number} {title}")
        else:
            try:
                merge_pr(env, pr)
                log(f"  merged      #{number} {title}")
            except HttpFailure as failure:
                hint = " (GH_TOKEN may not be allowed to merge into the default branch)" \
                    if failure.code in (401, 403) else ""
                log(f"  not merged  #{number} {title}: {failure}{hint}")
    print(json.dumps(items))
    return 0


if __name__ == "__main__":
    sys.exit(main())
