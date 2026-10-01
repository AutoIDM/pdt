"""Turn Claude's verdict into a tier label and a comment on the pull request.

This is the ``check`` hook of the score-prs task. It reads
``{"item", "result", "job_url"}`` on stdin. The item came from select_prs.py
and carries the rule floor; the result is Claude's JSON output, whose text
should end with a fenced json block ``{"tier": ..., "reasons": [...]}``.

The tier is the higher of the floor and Claude's answer. Claude can raise
the floor, never lower it. No usable verdict (an error, no JSON, an unknown
tier) counts as unavailable: a simple floor becomes review, because nothing
merges on the rules alone.

Writes, unless DRY_RUN is anything but the exact word ``false``:
  the ``tier::<tier>`` label (the other two tier labels are removed, and a
  missing label is created), and one comment whose first line is a marker
  select_prs.py reads next time. The comment is posted only when the marker
  would differ from the previous score's.

Verdict printed for the runner: ``ok`` for simple and review; ``needs_human``
for architectural, so the workflow run shows a warning and the summary lists
the PRs to discuss, and for an unavailable verdict. Never ``error`` for a
score.

Environment:
  GH_TOKEN, DRY_RUN, GITHUB_REPOSITORY, GITHUB_API_URL   as in select_prs.py.

Stdlib only.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from urllib.parse import quote
from dataclasses import dataclass

TIERS = ("simple", "review", "architectural")
MARKER_PREFIX = "<!-- pdt-pr-score v1 "
LABEL_COLORS = {"simple": "2da44e", "review": "d4a72c", "architectural": "cf222e"}
AGREE = "agree with floor"
NO_VERDICT = "Claude gave no verdict"
TIMEOUT = 60
DEFAULT_API = "https://api.github.com"
MERGE_FOOTER = ("pdt CI merges this PR once every check passes with no conflicts and no "
                "unresolved review threads.")
FOOTER = "A changed diff is scored again."


class HttpFailure(Exception):
    """An HTTP call answered with an error status."""

    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


@dataclass
class Score:
    tier: str
    floor: str
    reasons: list[str]
    claude: str  # agree | raised | unavailable


def log(text: str) -> None:
    print(text, file=sys.stderr)


# --- pure parts ---------------------------------------------------------------

def rank(tier: str) -> int:
    return TIERS.index(tier)


def parse_verdict(result: dict) -> dict | None:
    """Claude's ``{"tier", "reasons"}`` from its result, or None when unusable."""
    if not isinstance(result, dict) or result.get("is_error"):
        return None
    text = str(result.get("result") or "")
    fenced = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if fenced:
        raw = fenced[-1]
    else:
        start, end = text.rfind("{"), text.rfind("}")
        if start < 0 or end < start:
            return None
        raw = text[start:end + 1]
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or data.get("tier") not in TIERS:
        return None
    reasons = data.get("reasons")
    if not isinstance(reasons, list) or not all(isinstance(r, str) for r in reasons):
        return None
    return {"tier": data["tier"], "reasons": [r.strip() for r in reasons if r.strip()][:4]}


def combine(floor: str, floor_reasons: list[str], verdict: dict | None) -> Score:
    """The higher of the floor and Claude's tier. Claude never lowers the floor."""
    if verdict is None:
        if floor == "simple":
            return Score("review", floor,
                         [*floor_reasons, f"{NO_VERDICT}, so simple needs a human"], "unavailable")
        return Score(floor, floor, [*floor_reasons, NO_VERDICT], "unavailable")
    claude_reasons = [r for r in verdict["reasons"] if r.lower().rstrip(".") != AGREE]
    if rank(verdict["tier"]) > rank(floor):
        return Score(verdict["tier"], floor, claude_reasons or floor_reasons, "raised")
    return Score(floor, floor, [*floor_reasons, *claude_reasons], "agree")


def format_marker(diff: str, floor: str, tier: str, claude: str) -> str:
    return f"{MARKER_PREFIX}diff={diff} floor={floor} tier={tier} claude={claude} -->"


def marker_fields(diff: str, score: Score) -> dict:
    return {"diff": diff, "floor": score.floor, "tier": score.tier, "claude": score.claude}


def comment_body(diff: str, score: Score, job_url: str) -> str:
    if score.claude == "agree":
        how = "rules and Claude agree"
    elif score.claude == "raised":
        how = f"rules said {score.floor}; Claude raised it"
    else:
        how = f"rules said {score.floor}; {NO_VERDICT}"
    lines = [format_marker(diff, score.floor, score.tier, score.claude),
             f"**PR tier: `tier::{score.tier}`** ({how})", ""]
    lines += [f"- {reason}" for reason in score.reasons]
    footer = f"{MERGE_FOOTER} {FOOTER}" if score.tier == "simple" else FOOTER
    lines += ["", footer + (f" Job log: {job_url}" if job_url else "")]
    return "\n".join(lines)


def status_for(score: Score) -> dict:
    if score.claude == "unavailable":
        return {"status": "needs_human",
                "message": f"{NO_VERDICT}; scored {score.tier} from the rules alone"}
    if score.tier == "architectural":
        first = score.reasons[0] if score.reasons else "see the PR comment"
        return {"status": "needs_human", "message": f"needs discussion: {first}"}
    return {"status": "ok", "message": f"tier::{score.tier}"}


def label_change(current: list[str], tier: str) -> tuple[list[str], list[str]]:
    """(add, remove) to leave exactly one tier label on the PR."""
    want = f"tier::{tier}"
    remove = [name for name in current if name.startswith("tier::") and name != want]
    add = [] if want in current else [want]
    return add, remove


# --- GitHub -------------------------------------------------------------------

def api(env, method: str, path: str, payload: dict | None = None):
    api_url = env.get("GITHUB_API_URL", DEFAULT_API).rstrip("/")
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        f"{api_url}/repos/{env['GITHUB_REPOSITORY']}{path}", method=method, data=data,
        headers={"Authorization": f"Bearer {env['GH_TOKEN']}",
                 "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            body = response.read()
    except urllib.error.HTTPError as error:
        detail = error.read().decode(errors="replace")[:300]
        raise HttpFailure(error.code, f"{method} {path} returned {error.code}: {detail}") from None
    return json.loads(body) if body.startswith((b"{", b"[")) else body.decode()


def ensure_label(env, tier: str) -> None:
    name = f"tier::{tier}"
    try:
        api(env, "GET", f"/labels/{quote(name, safe='')}")
        return
    except HttpFailure as failure:
        if failure.code != 404:
            raise
    api(env, "POST", "/labels",
        {"name": name, "color": LABEL_COLORS[tier], "description": f"pdt PR score: {tier}"})


def set_label(env, number: int, add: list[str], remove: list[str]) -> None:
    if add:
        api(env, "POST", f"/issues/{number}/labels", {"labels": add})
    for name in remove:
        api(env, "DELETE", f"/issues/{number}/labels/{quote(name, safe='')}")


def post_comment(env, number: int, body: str) -> None:
    api(env, "POST", f"/issues/{number}/comments", {"body": body})


# --- orchestration ------------------------------------------------------------

def main() -> int:
    payload = json.load(sys.stdin)
    item, result, job_url = payload["item"], payload["result"], payload.get("job_url", "")
    env = os.environ
    dry_run = env.get("DRY_RUN", "true").strip().lower() != "false"
    number = int(item["number"])

    score = combine(item["floor"], list(item.get("floor_reason_list") or []),
                    parse_verdict(result))
    add, remove = label_change(list(item.get("labels") or []), score.tier)
    previous = item.get("previous") or {}
    fields = marker_fields(item["diff_id"], score)
    changed = any(previous.get(key) != value for key, value in fields.items())
    log(f"#{number} tier {score.tier} (floor {score.floor}, Claude {score.claude})")

    if dry_run:
        if add or remove:
            log(f"dry run: would set label tier::{score.tier}")
        if changed:
            log("dry run: would post the score comment")
    elif env.get("GH_TOKEN") and env.get("GITHUB_REPOSITORY"):
        if add:
            ensure_label(env, score.tier)
        if add or remove:
            set_label(env, number, add, remove)
            log(f"set label tier::{score.tier}")
        if changed:
            post_comment(env, number, comment_body(item["diff_id"], score, job_url))
            log("posted the score comment")
    else:
        log("GH_TOKEN or GITHUB_REPOSITORY is not set; nothing written")

    print(json.dumps(status_for(score)))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except HttpFailure as failure:
        log(str(failure))
        sys.exit(1)
