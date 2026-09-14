"""The rebase-mrs task hooks: which MRs qualify and how a push is judged."""

import importlib.util
import sys
from pathlib import Path

TASK = Path(__file__).resolve().parent.parent / "claude-tasks" / "rebase-mrs"


def load_script(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


select = load_script(TASK / "select_mrs.py", "rebase_select")
check = load_script(TASK / "check_mrs.py", "rebase_check")


def mr(**overrides):
    base = {"iid": 5, "title": "Add thing", "source_project_id": 100,
            "source_branch": "thing", "target_branch": "master", "labels": [], "draft": False}
    return {**base, **overrides}


def test_wanted_keeps_same_project_and_drafts():
    assert select.wanted(mr(), 100)
    assert select.wanted(mr(draft=True, title="Draft: x"), "100")


def test_wanted_drops_forks_and_opted_out():
    assert not select.wanted(mr(source_project_id=999), 100)
    assert not select.wanted(mr(labels=["no-autorebase"]), 100)


def test_item_carries_what_prompt_and_check_need():
    item = select.item_for(mr(), "abc123", 3)
    assert item["id"] == 5 and item["iid"] == 5
    assert item["source_branch"] == "thing" and item["target_branch"] == "master"
    assert item["before_sha"] == "abc123" and item["ahead"] == 3


def test_classify_unchanged_branch_needs_human():
    status, message = check.classify("old", "old", "main", "base", 3, 3)
    assert status == "needs_human" and "did not push" in message


def test_classify_clean_rebase_is_ok():
    assert check.classify("old", "new", "main", "main", 3, 3)[0] == "ok"
    assert check.classify("old", "new", "main", "main", 3, 2)[0] == "ok"


def test_classify_extra_commits_or_wrong_base_needs_human():
    status, message = check.classify("old", "new", "main", "main", 3, 4)
    assert status == "needs_human" and "old" in message
    status, message = check.classify("old", "new", "main", "other", 3, 3)
    assert status == "needs_human" and "old" in message
    assert check.classify("old", "new", "main", "main", 3, 0)[0] == "needs_human"


def test_comment_only_when_someone_must_look():
    assert check.comment_for("ok", "rebased", "Pushed. No conflicts. Tests pass.", "http://j") == ""
    reviewed = check.comment_for("ok", "rebased", "Resolved a conflict in cli.py.", "http://j")
    assert "review" in reviewed and "http://j" in reviewed
    failed = check.comment_for("needs_human", "did not push", "gave up", "http://j")
    assert "could not rebase" in failed and "http://j" in failed and "gave up" in failed


def test_mentions_conflicts():
    assert check.mentions_conflicts("There was a conflict in a.py")
    assert not check.mentions_conflicts("Rebased cleanly, no conflicts.")
    assert not check.mentions_conflicts("")
