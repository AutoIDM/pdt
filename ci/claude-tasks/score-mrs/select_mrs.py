"""Floor every open merge request and merge the simple ones already scored.

This is the ``select`` hook of the score-mrs task. For each open merge
request from this project it reads the diff from the GitLab API, sets a floor
tier from path rules, and looks for the score note a previous run left.

  Not scored yet, or the diff changed since: the MR becomes one item, so
  Claude reads it and may raise the floor (see check_mrs.py).
  Already scored for this diff: nothing for Claude. When the tier is
  ``simple`` and the MR is mergeable (pipeline passed on the head commit, no
  conflicts, no unresolved discussions) it is merged right here, so a green
  pipeline never waits for another Claude run.
  A Draft is scored once and then left alone while it stays a Draft, however
  often it changes. When it leaves Draft it is scored again only if its diff
  changed since. A Draft is never merged.

The floor is deterministic and default-deny: a path no rule names is
``review``. The three tiers, lowest first, are simple, review, architectural.

It also fetches every source branch into the checkout, so Claude's session
can run ``git diff origin/<target>...origin/<branch>`` without fetching.

A pipeline the project webhook started (through the trigger API) is about
one merge request. The webhook's custom template passes the MR number as
the pipeline variable MR_IID, and such a run looks at that MR alone. Which
events run this task at all is decided by the score-mrs job rules in
.gitlab-ci.yml (on the MR_ACTION and MR_DRAFT_* variables from the same
template): only the MR opening or leaving Draft. Every other event runs the
empty score-mrs-skip job, because a trigger call that creates no pipeline
counts as a webhook failure and GitLab disables the hook. A schedule or web
run has no MR_IID and sweeps every open MR, which also picks up a changed
diff.

Environment:
  GITLAB_TOKEN   project access token with ``api``. Merging into the default
                 branch needs the token's role to be allowed to merge there.
  DRY_RUN        anything but the exact word ``false`` means list and change
                 nothing; the CI job sets it to false on master and schedules.
  MR_IID         set by the webhook's template on a trigger pipeline; the
                 run scores that MR alone. Absent on a schedule or web run.
  CI_API_V4_URL, CI_PROJECT_ID, CI_DEFAULT_BRANCH   set by GitLab CI.

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
MARKER_PREFIX = "<!-- pdt-mr-score v1 "
MAX_SIMPLE_FILES = 5
MAX_SIMPLE_LINES = 200
TIMEOUT = 60
DEFAULT_API = "https://gitlab.com/api/v4"
REQUIRED = ("GITLAB_TOKEN", "CI_PROJECT_ID")
MERGE_NOTE = ("Auto-merged as `tier::simple` by pdt CI: the rules and Claude both scored "
              "it simple, and its pipeline passed with no conflicts and no open discussions.")

# Path rules, first match wins, checked in this order after the pyproject rule.
ARCHITECTURAL = (
    ("src/pdt/utils/*", "public API that deployed apps import"),
    ("src/pdt/deploy.py", "provider-neutral deploy flow"),
    ("src/pdt/deploy_common.py", "shared by every provider"),
    ("src/pdt/config.py", "config loading and validation"),
    ("src/pdt/cli.py", "the command line"),
    ("src/pdt/__init__.py", "package root"),
    (".gitlab-ci.yml", "CI rules"),
    (".github/workflows/*", "publishing workflow"),
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
# A few changed lines in one of these files floor at simple when the MR also
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


def rule_floor(mr: dict, files: list[dict]) -> tuple[str, list[str]]:
    """The floor tier of a merge request and the reasons at that tier."""
    found: list[tuple[str, str]] = []
    lines = 0
    tested = any(fnmatch(file.get("new_path") or "", "tests/*") for file in files)
    for file in files:
        path = file.get("new_path") or file.get("old_path") or ""
        diff = file.get("diff") or ""
        lines += len(changed_lines(diff))
        found.append(path_floor(path, bool(file.get("new_file")), diff, tested))
        if file.get("deleted_file"):
            found.append(("review", f"{path} is deleted"))
        if file.get("renamed_file"):
            found.append(("review", f"{path} is renamed"))
        if file.get("too_large") or file.get("collapsed"):
            found.append(("review", f"{path} diff is too large to inspect"))
        for word in hard_gate_hits(diff):
            found.append(("review", f"{path} mentions `{word}`"))
    if len(files) > MAX_SIMPLE_FILES:
        found.append(("review", f"{len(files)} files changed, more than {MAX_SIMPLE_FILES}"))
    if lines > MAX_SIMPLE_LINES:
        found.append(("review", f"{lines} lines changed, more than {MAX_SIMPLE_LINES}"))
    if str(mr.get("changes_count", "")).endswith("+"):
        found.append(("review", "GitLab truncated the diff"))
    if str(mr.get("source_project_id")) != str(mr.get("target_project_id")):
        found.append(("review", "comes from a fork"))
    floor = highest("simple", *(tier for tier, _ in found))
    reasons = [why for tier, why in found if tier == floor and why]
    if floor == "simple":
        reasons = ["only documentation, tests, or example apps changed"]
        small = [file["new_path"] for file in files
                 if small_edit(file.get("new_path") or "", file.get("diff") or "")]
        if small:
            reasons = [f"{', '.join(small)} changes at most {MAX_SMALL_EDIT_LINES} lines "
                       "and the MR changes a test"]
    return floor, reasons


def diff_id(files: list[dict]) -> str:
    """A short id for the diff's content. A clean rebase keeps it the same."""
    digest = hashlib.sha256()
    for file in sorted(files, key=lambda f: f.get("new_path") or f.get("old_path") or ""):
        path = file.get("new_path") or file.get("old_path") or ""
        digest.update(f"{path}\n{file.get('diff') or ''}\n".encode())
    return digest.hexdigest()[:12]


def merge_blockers(mr: dict) -> list[str]:
    """Why a simple MR cannot be merged right now. Empty means go."""
    blockers = []
    if mr.get("draft"):
        blockers.append("still a draft")
    if mr.get("has_conflicts"):
        blockers.append("has conflicts")
    if not mr.get("blocking_discussions_resolved", True):
        blockers.append("unresolved discussions")
    pipeline = mr.get("head_pipeline") or {}
    if not pipeline:
        blockers.append("pipeline missing")
    elif pipeline.get("status") != "success":
        blockers.append(f"pipeline {pipeline.get('status')}")
    elif pipeline.get("sha") != mr.get("sha"):
        blockers.append("pipeline ran on an older commit")
    if mr.get("detailed_merge_status", "mergeable") != "mergeable":
        blockers.append(f"merge status {mr.get('detailed_merge_status')}")
    if str(mr.get("source_project_id")) != str(mr.get("target_project_id")):
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


def previous_score(notes: list[dict]) -> dict | None:
    """The newest score note's marker. Notes arrive newest first."""
    for note in notes:
        if note.get("system"):
            continue
        marker = parse_marker(note.get("body", ""))
        if marker:
            return marker
    return None


def wanted(mr: dict, project_id) -> bool:
    """Same-project MRs only. Drafts stay in; they are scored once."""
    return str(mr.get("source_project_id")) == str(project_id)


def already_scored(mr: dict, previous: dict | None, did: str) -> bool:
    """Nothing for Claude: scored for this diff, or a Draft scored at any diff."""
    if previous is None or previous["claude"] == "unavailable":
        return False
    return previous["diff"] == did or bool(mr.get("draft"))


def item_for(mr: dict, did: str, floor: str, reasons: list[str], files: list[dict],
             previous: dict | None) -> dict:
    listed = []
    for file in files:
        path = file.get("new_path") or file.get("old_path") or ""
        flags = [name for name in ("new_file", "deleted_file", "renamed_file") if file.get(name)]
        listed.append(path + (f" ({', '.join(flags)})" if flags else ""))
    return {
        "id": mr["iid"],
        "iid": mr["iid"],
        "title": mr["title"],
        "source_branch": mr["source_branch"],
        "target_branch": mr["target_branch"],
        "head_sha": mr.get("sha", ""),
        "diff_id": did,
        "floor": floor,
        "floor_reasons": "; ".join(reasons),
        "floor_reason_list": reasons,
        "files": "\n".join(listed),
        "labels": list(mr.get("labels") or []),
        "previous": previous,
    }


# --- GitLab -------------------------------------------------------------------

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


def api(env, method: str, path: str, payload: dict | None = None):
    api_url = env.get("CI_API_V4_URL", DEFAULT_API).rstrip("/")
    return http(method, f"{api_url}{path}",
                {"Authorization": f"Bearer {env['GITLAB_TOKEN']}"}, payload)


def paginate(env, path: str) -> list:
    """GET every page of ``path`` (which already holds a query string)."""
    items: list = []
    page = "1"
    while page:
        found, headers = api(env, "GET", f"{path}&page={page}")
        items.extend(found)
        page = headers.get("X-Next-Page", "") or ""
    return items


def list_open_mrs(env, target: str) -> list[dict]:
    return paginate(env, f"/projects/{env['CI_PROJECT_ID']}/merge_requests"
                         f"?state=opened&target_branch={target}&per_page=100")


def get_mr(env, iid: int) -> dict:
    """One merge request in full. The list endpoint leaves out head_pipeline
    and changes_count, which the merge gates and the rules read."""
    return api(env, "GET", f"/projects/{env['CI_PROJECT_ID']}/merge_requests/{iid}")[0]


def list_diffs(env, iid: int) -> list[dict]:
    return paginate(env, f"/projects/{env['CI_PROJECT_ID']}/merge_requests/{iid}/diffs?per_page=100")


def list_notes(env, iid: int) -> list[dict]:
    return paginate(env, f"/projects/{env['CI_PROJECT_ID']}/merge_requests/{iid}/notes"
                         f"?sort=desc&order_by=created_at&per_page=100")


def merge_mr(env, mr: dict) -> None:
    base = f"/projects/{env['CI_PROJECT_ID']}/merge_requests/{mr['iid']}"
    api(env, "PUT", f"{base}/merge",
        {"sha": mr["sha"], "should_remove_source_branch": True, "squash": False})
    api(env, "POST", f"{base}/notes", {"body": MERGE_NOTE})


def fetch_branches(branches: list[str]) -> None:
    refspecs = [f"+refs/heads/{name}:refs/remotes/origin/{name}" for name in branches]
    subprocess.run(["git", "fetch", "--quiet", "origin", *refspecs], check=True)


# --- orchestration ------------------------------------------------------------

def main() -> int:
    env = os.environ
    missing = [name for name in REQUIRED if not env.get(name)]
    if missing:
        log(f"set {', '.join(missing)} before running the score-mrs task")
        return 1
    dry_run = env.get("DRY_RUN", "true").strip().lower() != "false"
    target = env.get("CI_DEFAULT_BRANCH", "master")

    mrs = [mr for mr in list_open_mrs(env, target) if wanted(mr, env["CI_PROJECT_ID"])]
    event_iid = env.get("MR_IID", "").strip()
    if event_iid:
        mrs = [mr for mr in mrs if str(mr["iid"]) == event_iid]
        log(f"webhook event for !{event_iid}: looking at that MR only"
            + ("" if mrs else " (it is not an open MR of this project)"))
    log(f"{len(mrs)} open MR(s) targeting {target} qualify"
        + (" (dry run: nothing is merged)" if dry_run else ""))
    if mrs:
        fetch_branches([target, *(mr["source_branch"] for mr in mrs)])

    items = []
    for listed in mrs:
        mr = get_mr(env, listed["iid"])
        files = list_diffs(env, mr["iid"])
        did = diff_id(files)
        floor, reasons = rule_floor(mr, files)
        previous = previous_score(list_notes(env, mr["iid"]))
        if not already_scored(mr, previous, did):
            log(f"  score       !{mr['iid']} {mr['title']} (floor {floor}: {'; '.join(reasons)})")
            items.append(item_for(mr, did, floor, reasons, files, previous))
            continue
        tier = previous["tier"]
        if mr.get("draft"):
            log(f"  draft       !{mr['iid']} {mr['title']} ({tier}, scored once; not merged)")
            continue
        if tier != "simple":
            log(f"  {tier:11} !{mr['iid']} {mr['title']} (already scored)")
            continue
        blockers = merge_blockers(mr)
        if blockers:
            log(f"  blocked     !{mr['iid']} {mr['title']} (simple, but {', '.join(blockers)})")
        elif dry_run:
            log(f"  would merge !{mr['iid']} {mr['title']}")
        else:
            try:
                merge_mr(env, mr)
                log(f"  merged      !{mr['iid']} {mr['title']}")
            except HttpFailure as failure:
                hint = " (GITLAB_TOKEN may not be allowed to merge into the default branch)" \
                    if failure.code in (401, 403) else ""
                log(f"  not merged  !{mr['iid']} {mr['title']}: {failure}{hint}")
    print(json.dumps(items))
    return 0


if __name__ == "__main__":
    sys.exit(main())
