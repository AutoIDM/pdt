"""The rebase-mrs task hooks: which MRs qualify and how a push is judged."""

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

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


# --- clean rebases with git alone --------------------------------------------

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


def test_keeps_commits():
    assert select.keeps_commits(3, 3)
    assert select.keeps_commits(2, 3)  # one commit was already on the base and got dropped
    assert not select.keeps_commits(0, 3)  # the whole branch is already merged
    assert not select.keeps_commits(4, 3)


class Repo:
    """A bare origin and a clone with `master`, plus branches off an older master."""

    def __init__(self, root: Path):
        self.origin = root / "origin.git"
        self.clone = root / "clone"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "master", self.origin], check=True)
        subprocess.run(["git", "clone", "-q", self.origin, self.clone], check=True,
                       capture_output=True)
        self.git("config", "user.name", "t")
        self.git("config", "user.email", "t@x")
        self.commit("base.txt", "base", "base")
        self.git("push", "-q", "origin", "master")

    def git(self, *args: str) -> str:
        return subprocess.run(["git", *args], cwd=self.clone, check=True,
                              capture_output=True, text=True).stdout.strip()

    def commit(self, name: str, text: str, message: str) -> None:
        (self.clone / name).write_text(text)
        self.git("add", name)
        self.git("commit", "-q", "-m", message)

    def branch(self, name: str, files: dict[str, str]) -> None:
        self.git("checkout", "-q", "-b", name, "origin/master")
        for filename, text in files.items():
            self.commit(filename, text, f"{name}: {filename}")
        self.git("push", "-q", "origin", name)

    def advance_master(self, name: str, text: str) -> None:
        self.git("checkout", "-q", "master")
        self.commit(name, text, f"master: {name}")
        self.git("push", "-q", "origin", "master")
        self.git("fetch", "-q", "origin")

    def sha(self, ref: str) -> str:
        return self.git("rev-parse", ref)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    built = Repo(tmp_path)
    monkeypatch.chdir(built.clone)
    return built


@needs_git
def test_clean_branch_is_rebased_and_pushed_without_claude(repo):
    repo.branch("clean", {"a.txt": "a", "b.txt": "b"})
    before = repo.sha("origin/clean")
    repo.advance_master("base.txt", "base changed")
    assert select.rebase_with_git("master", "clean", before, 2, push=True)
    repo.git("fetch", "-q", "origin")
    assert repo.git("merge-base", "origin/master", "origin/clean") == repo.sha("origin/master")
    assert repo.git("rev-list", "--count", "origin/master..origin/clean") == "2"
    assert repo.git("status", "--porcelain") == ""  # the clone itself was not touched
    assert "pdt-rebase-" not in repo.git("worktree", "list")


@needs_git
def test_dry_run_reports_a_clean_rebase_but_pushes_nothing(repo):
    repo.branch("clean", {"a.txt": "a"})
    before = repo.sha("origin/clean")
    repo.advance_master("base.txt", "base changed")
    assert select.rebase_with_git("master", "clean", before, 1, push=False)
    repo.git("fetch", "-q", "origin")
    assert repo.sha("origin/clean") == before


@needs_git
def test_conflict_goes_to_claude_and_leaves_no_worktree(repo):
    repo.branch("clash", {"base.txt": "branch side"})
    before = repo.sha("origin/clash")
    repo.advance_master("base.txt", "master side")
    assert not select.rebase_with_git("master", "clash", before, 1, push=True)
    repo.git("fetch", "-q", "origin")
    assert repo.sha("origin/clash") == before
    assert "pdt-rebase-" not in repo.git("worktree", "list")


@needs_git
def test_branch_already_merged_in_substance_goes_to_claude(repo):
    repo.branch("dup", {"same.txt": "same"})
    before = repo.sha("origin/dup")
    repo.advance_master("same.txt", "same")
    assert not select.rebase_with_git("master", "dup", before, 1, push=True)


@needs_git
def test_moved_branch_is_not_overwritten(repo):
    repo.branch("moved", {"a.txt": "a"})
    stale_sha = repo.sha("origin/moved")
    repo.commit("a2.txt", "a2", "moved: a2")  # the author pushed again meanwhile
    repo.git("push", "-q", "origin", "moved")
    repo.advance_master("base.txt", "base changed")
    assert not select.rebase_with_git("master", "moved", stale_sha, 1, push=True)
