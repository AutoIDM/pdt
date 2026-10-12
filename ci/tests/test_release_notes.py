"""release_notes.py: grouping by Conventional Commits type and the summary."""

import importlib.util
import json
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


def test_a_bot_and_an_empty_author_get_no_credit():
    items = [change("fix: a", 1), change("fix: b", 2), change("fix: c", 3, author="dependabot[bot]"),
             change("fix: d", None, author="")]
    assert "by @" not in notes.render(items, REPO, "v0.1.9", "v0.2.0")


def test_no_previous_tag_omits_the_compare_link():
    assert "compare" not in notes.render([change("fix: a", 1)], REPO, "", "v0.1.0")


def fake_gh(responses):
    def gh(path, *args):
        return responses[path]
    return gh


def pr(number, title, author="jon"):
    return {"number": number, "title": title, "body": None, "user": {"login": author},
            "merged_at": "2026-10-12T00:00:00Z"}


def commit(message, files, author="jon"):
    return json.dumps({"commit": {"message": message}, "author": {"login": author} if author else None,
                       "files": [{"status": "modified", "filename": name} for name in files]})


def test_a_merge_commit_pr_is_listed_once_with_the_merge_commit_files(monkeypatch):
    merged = json.dumps([pr(20, "feat(gui): add a page")])
    responses = {
        "repos/AutoIDM/pdt/compare/v0.1.9...v0.2.0?per_page=100": "s1\nb1\nb2\nb3\nm1\nd1\n",
        "repos/AutoIDM/pdt/commits/s1/pulls": json.dumps([pr(19, "fix: squash", "visch")]),
        "repos/AutoIDM/pdt/commits/s1": commit("fix: squash (#19)", ["a.py"]),
        "repos/AutoIDM/pdt/commits/d1/pulls": "[]",
        "repos/AutoIDM/pdt/commits/d1": commit("docs: direct push\n\nBody.", ["README.md"], author=""),
        "repos/AutoIDM/pdt/commits/m1/pulls": merged,
        "repos/AutoIDM/pdt/commits/m1": commit("Merge pull request #20", ["b.py", "c.py", "d.py"]),
    }
    for sha in ("b1", "b2", "b3"):
        responses[f"repos/AutoIDM/pdt/commits/{sha}/pulls"] = merged
    monkeypatch.setattr(notes, "gh", fake_gh(responses))
    changes = notes.fetch(REPO, "v0.1.9", "v0.2.0")
    assert [(c.title, c.number, c.author) for c in changes] == [
        ("docs: direct push", None, ""), ("feat(gui): add a page", 20, "jon"), ("fix: squash", 19, "visch")]
    assert [path for _, path in changes[1].files] == ["b.py", "c.py", "d.py"]


def test_no_previous_tag_lists_the_history_of_the_tag(monkeypatch):
    monkeypatch.setattr(notes, "gh", fake_gh({
        "repos/AutoIDM/pdt/commits?sha=v0.1.0&per_page=100": "c2\nc1\n",
        "repos/AutoIDM/pdt/commits/c2/pulls": json.dumps([pr(2, "feat: second")]),
        "repos/AutoIDM/pdt/commits/c2": commit("feat: second (#2)", ["a.py"]),
        "repos/AutoIDM/pdt/commits/c1/pulls": json.dumps([pr(1, "feat: first")]),
        "repos/AutoIDM/pdt/commits/c1": commit("feat: first (#1)", ["a.py"]),
    }))
    assert [c.number for c in notes.fetch(REPO, "", "v0.1.0")] == [2, 1]
