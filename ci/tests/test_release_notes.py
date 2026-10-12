"""release_notes.py: grouping by Conventional Commits type and the summary."""

import importlib.util
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "release_notes.py"

spec = importlib.util.spec_from_file_location("release_notes", SCRIPT)
notes = importlib.util.module_from_spec(spec)
sys.modules["release_notes"] = notes
spec.loader.exec_module(notes)

REPO = "AutoIDM/pdt"
SUMMARY = "pdt 0.2.0 runs faster.\n\n- **Speed.** Deploys take half the time."


def change(title, number, body="", author="jon", files=(("modified", "src/pdt/cli.py"),)):
    return notes.Change(title, number, body, author, list(files))


def changes():
    return [
        change("docs: fix a typo", 1),
        change("feat(deploy)!: build images locally", 2),
        change("fix(logs): print lines in order", 3),
        change("feat(gui): add pdt gui", 4),
        change("refactor(config): drop a helper", 5, body="Text.\n\nBREAKING CHANGE: the key is gone."),
        change("Add license-waste example", 6, author="visch",
               files=[("modified", "README.md"), ("added", "src/pdt/examples/license-waste/run.ps1")]),
        change("feat(examples): add a column to hello-world", 7,
               files=[("modified", "src/pdt/examples/hello-world/run.py")]),
        change("perf(storage): read state once", 8),
        change("test(cli): cover --json", 9),
        change("ci(verify): run on main", 10),
        change("build: pin uv", 11),
        change("Update README", 12, author="visch", files=[("modified", "README.md")]),
        change("chore(release): 0.2.0", 13, body=f"## Summary\n\n{SUMMARY}\n\n## Files\n\nfour files"),
    ]


def test_every_change_is_listed_once_in_section_order(capsys):
    text = notes.render(changes(), REPO, "v0.1.9", "v0.2.0")
    assert text == f"""## Summary

{SUMMARY}

## Breaking changes

- feat(deploy)!: build images locally (#2)
- refactor(config): drop a helper (#5)

## Features

- feat(gui): add pdt gui (#4)

## Fixes

- fix(logs): print lines in order (#3)

## Performance

- perf(storage): read state once (#8)

## Examples

- Add license-waste example (#6), by @visch
- feat(examples): add a column to hello-world (#7)

## Documentation

- docs: fix a typo (#1)

## Tests

- test(cli): cover --json (#9)

## CI and release

- ci(verify): run on main (#10)
- build: pin uv (#11)
- chore(release): 0.2.0 (#13)

## Other

- Update README (#12), by @visch

**Full changelog:** https://github.com/AutoIDM/pdt/compare/v0.1.9...v0.2.0
"""
    assert capsys.readouterr().err == ""


def test_a_release_pr_without_a_summary_warns_and_starts_with_the_list(capsys):
    items = changes()
    items[-1].body = "Bumps the version."
    text = notes.render(items, REPO, "v0.1.9", "v0.2.0")
    assert text.startswith("## Breaking changes\n")
    assert capsys.readouterr().err.startswith("::warning title=Release notes::")
