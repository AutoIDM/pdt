"""Write the release notes for one tag, grouped by Conventional Commits type.

The notes list every pull request merged between the previous tag and this
one, newest first and once each, under the headings in SECTIONS. With no
previous tag they list the whole history of this tag and have no compare
link. A breaking change (``!`` before the colon, or a ``BREAKING CHANGE:``
footer in the PR body) goes under "Breaking changes" only. A PR whose
files are all under EXAMPLES, or a PR with no type that adds a file there,
goes under "Examples". The author of the most PRs in the range is the
maintainer; any other author gets ", by @login" on the line, except a bot
(a login ending in ``[bot]``) or a commit with no GitHub user.

The summary at the top is the ``## Summary`` section of the body of the
``chore(release): X.Y.Z`` PR, written by a person. When it is missing the
notes start with the grouped list, and a ``::warning::`` annotation says so.

Usage: ci/release_notes.py OWNER/REPO PREVIOUS_TAG TAG > release-notes.md
PREVIOUS_TAG may be "" for the first release.

Reads GitHub through ``gh api``, so GH_TOKEN must be set in CI.
Stdlib only, so CI runs it with no extra install.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass

EXAMPLES = "src/pdt/examples/"
SECTIONS = ["Breaking changes", "Features", "Fixes", "Performance", "Examples", "Documentation",
            "Tests", "Refactoring", "CI and release", "Other"]
TYPE_SECTION = {"feat": "Features", "fix": "Fixes", "perf": "Performance", "docs": "Documentation",
                "test": "Tests", "refactor": "Refactoring", "ci": "CI and release",
                "build": "CI and release", "chore": "CI and release"}
HEADER = re.compile(r"^(\w+)(\([^)]*\))?(!)?: ")
BREAKING_FOOTER = re.compile(r"^BREAKING[ -]CHANGE: ", re.MULTILINE)


@dataclass
class Change:
    title: str
    number: int | None
    body: str
    author: str
    files: list[tuple[str, str]]


def section(change: Change) -> str:
    header = HEADER.match(change.title)
    if (header and header[3]) or BREAKING_FOOTER.search(change.body):
        return "Breaking changes"
    paths = [path for _, path in change.files]
    if paths and all(path.startswith(EXAMPLES) for path in paths):
        return "Examples"
    if not header:
        added = any(status == "added" and path.startswith(EXAMPLES) for status, path in change.files)
        return "Examples" if added else "Other"
    return TYPE_SECTION.get(header[1], "Other")


def summary(body: str) -> str:
    found = re.search(r"^## Summary[ \t]*\n(.*?)(?=^## |\Z)", body, re.MULTILINE | re.DOTALL)
    return found[1].strip() if found else ""


def render(changes: list[Change], repo: str, previous: str, tag: str) -> str:
    version = tag.removeprefix("v")
    release = next((c for c in changes if c.title == f"chore(release): {version}"), None)
    text = summary(release.body) if release else ""
    if not text:
        print(f"::warning title=Release notes::The chore(release): {version} PR body has no "
              "## Summary section, so the notes start with the list of changes.", file=sys.stderr)
    maintainer = Counter(c.author for c in changes).most_common(1)[0][0] if changes else ""
    parts = [f"## Summary\n\n{text}\n"] if text else []
    for heading in SECTIONS:
        lines = []
        for change in changes:
            if section(change) != heading:
                continue
            number = f" (#{change.number})" if change.number else ""
            credited = change.author not in ("", maintainer) and not change.author.endswith("[bot]")
            credit = f", by @{change.author}" if credited else ""
            lines.append(f"- {change.title}{number}{credit}")
        if lines:
            parts.append(f"## {heading}\n\n" + "\n".join(lines) + "\n")
    if previous:
        parts.append(f"**Full changelog:** https://github.com/{repo}/compare/{previous}...{tag}\n")
    return "\n".join(parts)


def gh(path: str, *args: str) -> str:
    return subprocess.run(["gh", "api", path, *args], stdout=subprocess.PIPE, encoding="utf-8",
                          check=True).stdout


def fetch(repo: str, previous: str, tag: str) -> list[Change]:
    if previous:
        shas = gh(f"repos/{repo}/compare/{previous}...{tag}?per_page=100", "--paginate",
                  "--jq", ".commits[].sha").split()[::-1]
    else:
        shas = gh(f"repos/{repo}/commits?sha={tag}&per_page=100", "--paginate", "--jq", ".[].sha").split()
    changes = []
    listed = set()
    for sha in shas:
        pulls = [p for p in json.loads(gh(f"repos/{repo}/commits/{sha}/pulls")) if p["merged_at"]]
        if pulls and pulls[0]["number"] in listed:
            continue
        commit = json.loads(gh(f"repos/{repo}/commits/{sha}"))
        files = [(f["status"], f["filename"]) for f in commit["files"]]
        if pulls:
            pr = pulls[0]
            listed.add(pr["number"])
            changes.append(Change(pr["title"], pr["number"], (pr["body"] or "").replace("\r\n", "\n"),
                                  pr["user"]["login"], files))
        else:
            subject, _, body = commit["commit"]["message"].partition("\n")
            author = (commit["author"] or {}).get("login", "")
            changes.append(Change(subject, None, body, author, files))
    return changes


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__, file=sys.stderr)
        return 1
    repo, previous, tag = argv
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stdout.write(render(fetch(repo, previous, tag), repo, previous, tag))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
