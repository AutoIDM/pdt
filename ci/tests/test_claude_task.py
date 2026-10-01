"""The generic Claude task runner: task folders, token checks, verdicts."""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import pytest

CI = Path(__file__).resolve().parent.parent


def load_script(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses resolve annotations through sys.modules
    spec.loader.exec_module(module)
    return module


runner = load_script(CI / "claude_task.py", "claude_task")


def make_task(tmp_path, config="", prompt="Do {thing}.", hooks=()):
    folder = tmp_path / "demo"
    folder.mkdir(parents=True)
    (folder / "task.yml").write_text(config)
    (folder / "prompt.md").write_text(prompt)
    for hook in hooks:
        (folder / hook).write_text("print('[]')\n")
    return tmp_path


# --- task folder --------------------------------------------------------------

def test_defaults_when_task_yml_is_empty(tmp_path):
    task = runner.load_task("demo", make_task(tmp_path))
    assert task.model == "opus"
    assert task.permission_mode == "acceptEdits"
    assert task.budget_usd == 20.0
    assert task.timeout_minutes == 30
    assert task.allowed_tools == []
    assert task.select is None and task.check is None and task.system is None


def test_task_yml_values_and_hooks(tmp_path):
    root = make_task(tmp_path, "model: sonnet\nbudget_usd: 2.5\ntimeout_minutes: 5\n"
                     "allowed_tools: [Read, 'Bash(git *)']\nselect: pick.py\ncheck: judge.py\n",
                     hooks=("pick.py", "judge.py"))
    (root / "demo" / "system.md").write_text("Be brief.")
    task = runner.load_task("demo", root)
    assert task.model == "sonnet"
    assert task.budget_usd == 2.5
    assert task.timeout_minutes == 5
    assert task.allowed_tools == ["Read", "Bash(git *)"]
    assert task.select.name == "pick.py"
    assert task.check.name == "judge.py"
    assert task.system.name == "system.md"


def test_hook_named_after_a_stdlib_module_is_rejected(tmp_path):
    root = make_task(tmp_path, "select: select.py\n", hooks=("select.py",))
    with pytest.raises(runner.TaskError, match="shadows Python's select module"):
        runner.load_task("demo", root)


@pytest.mark.parametrize("config, needle", [
    ("budget_usd: -1\n", "budget_usd"),
    ("timeout_minutes: zero\n", "timeout_minutes"),
    ("allowed_tools: Read\n", "allowed_tools"),
    ("select: missing.py\n", "missing.py"),
    ("colour: blue\n", "colour"),
])
def test_bad_task_yml_names_the_key(tmp_path, config, needle):
    with pytest.raises(runner.TaskError) as failure:
        runner.load_task("demo", make_task(tmp_path, config))
    assert needle in str(failure.value)
    assert "task.yml" in str(failure.value)


def test_missing_task_or_prompt(tmp_path):
    with pytest.raises(runner.TaskError, match="no task named"):
        runner.load_task("nope", tmp_path)
    (tmp_path / "bare").mkdir()
    (tmp_path / "bare" / "task.yml").write_text("")
    with pytest.raises(runner.TaskError, match="prompt.md"):
        runner.load_task("bare", tmp_path)


def test_render_prompt_fills_and_names_missing_fields():
    assert runner.render_prompt("Rebase {branch} for !{iid}", {"branch": "x", "iid": 7}) == \
        "Rebase x for !7"
    with pytest.raises(runner.TaskError, match="'branch'"):
        runner.render_prompt("Rebase {branch}", {"iid": 7})


def test_item_id_is_filesystem_safe():
    assert runner.item_id({"id": 42}, 0) == "42"
    assert runner.item_id({"id": "feat/x y"}, 0) == "feat_x_y"
    assert runner.item_id({}, 3) == "3"


# --- token verification -------------------------------------------------------

def test_missing_token_message_names_the_variable():
    problem = runner.missing_token_problem({})
    assert "CLAUDE_TOKEN is not set" in problem
    assert "handbook" in problem
    assert runner.missing_token_problem({"CLAUDE_TOKEN": "tok"}) == ""
    assert runner.missing_token_problem({"CLAUDE_CODE_OAUTH_TOKEN": "tok"}) == ""


def test_claude_env_maps_the_secret_name_to_the_name_claude_reads():
    env = runner.claude_env({"CLAUDE_TOKEN": "tok", "PATH": "/bin"})
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "tok"
    assert env["PATH"] == "/bin"


def test_auth_status_verdicts():
    good = json.dumps({"loggedIn": True, "authMethod": "claude.ai",
                       "email": "a@b.c", "subscriptionType": "max"})
    assert runner.auth_status_problem(good) == ""
    assert runner.auth_identity(good) == "a@b.c (max subscription)"
    from_env = json.dumps({"loggedIn": True, "authMethod": "oauth_token"})
    assert runner.auth_status_problem(from_env) == ""
    assert "oauth_token" in runner.auth_identity(from_env)
    assert "not logged in" in runner.auth_status_problem(json.dumps({"loggedIn": False}))
    assert "not a subscription token" in runner.auth_status_problem(
        json.dumps({"loggedIn": True, "authMethod": "apiKey"}))
    assert "no JSON" in runner.auth_status_problem("Not logged in\n")


def test_probe_verdicts():
    ok = json.dumps({"type": "result", "is_error": False, "result": "OK"})
    assert runner.probe_problem(0, ok) == ""
    rejected = json.dumps({"type": "result", "is_error": True,
                           "result": "API Error: 401 OAuth access token is invalid."})
    problem = runner.probe_problem(1, rejected)
    assert "Claude Code said: API Error: 401 OAuth access token is invalid." in problem
    assert "expired" not in problem  # no guessing at the cause
    # Claude Code's budget stop has no result text; the subtype is the message.
    budget = json.dumps({"type": "result", "is_error": True, "result": None,
                         "subtype": "error_max_budget_usd", "total_cost_usd": 0.1176})
    assert "Claude Code said: error_max_budget_usd" in runner.probe_problem(1, budget)
    assert "exited 124" in runner.probe_problem(124, "", "")
    assert "boom on stderr" in runner.probe_problem(1, "not json", "boom on stderr")
    assert runner.PROBE_BUDGET_USD >= 0.5


# --- results and verdicts -----------------------------------------------------

def test_parse_claude_result():
    good = runner.parse_claude_result(0, '{"is_error": false, "result": "done", "num_turns": 3}')
    assert good["num_turns"] == 3 and not good["is_error"]
    crash = runner.parse_claude_result(1, "Traceback ...")
    assert crash["is_error"] and "exited 1" in crash["result"]
    nonzero = runner.parse_claude_result(2, '{"result": "hm"}')
    assert nonzero["is_error"]


def test_default_status_without_a_check_hook():
    assert runner.default_status({"is_error": False}) == {"status": "ok", "message": ""}
    assert runner.default_status({"is_error": True, "result": "boom"})["status"] == "error"


def test_parse_check_output():
    assert runner.parse_check_output('{"status": "needs_human", "message": "look"}') == \
        {"status": "needs_human", "message": "look"}
    assert runner.parse_check_output("garbage")["status"] == "error"
    assert runner.parse_check_output('{"status": "maybe"}')["status"] == "error"


def test_exit_codes():
    assert runner.exit_code([]) == 0
    assert runner.exit_code(["ok", "ok"]) == 0
    assert runner.exit_code(["ok", "needs_human"]) == 2
    assert runner.exit_code(["needs_human", "error"]) == 1


def test_run_url_links_the_workflow_run():
    env = {"GITHUB_SERVER_URL": "https://github.com", "GITHUB_REPOSITORY": "AutoIDM/pdt",
           "GITHUB_RUN_ID": "77"}
    assert runner.run_url(env) == "https://github.com/AutoIDM/pdt/actions/runs/77"
    assert runner.run_url({}) == ""


def test_an_item_that_needs_a_person_prints_a_warning_annotation(tmp_path, monkeypatch, capsys):
    make_task(tmp_path)
    load = runner.load_task
    monkeypatch.setattr(runner, "load_task", lambda name: load(name, tmp_path))
    monkeypatch.setattr(runner, "verify_token", lambda task, env, report_dir: "")
    monkeypatch.setattr(runner, "REPORT_DIR", tmp_path / "report")
    monkeypatch.setattr(runner, "run_items", lambda *a: [
        {"id": "7", "status": "needs_human", "message": "needs discussion:\ncli.py"},
        {"id": "8", "status": "ok", "message": "tier::simple"}])
    assert runner.main(["demo"]) == runner.EXIT_NEEDS_HUMAN
    warnings = [line for line in capsys.readouterr().out.splitlines()
                if line.startswith("::warning")]
    assert warnings == ["::warning title=demo 7 needs a person::needs discussion: cli.py"]


def test_claude_command_shape(tmp_path):
    task = runner.load_task("demo", make_task(
        tmp_path, "allowed_tools: [Read, 'Bash(git *)']\ndisallowed_tools: ['Bash(git push -f*)']\n"))
    command = runner.claude_command(task)
    assert command[:2] == ["claude", "-p"]
    assert "--bare" not in command
    assert command[command.index("--model") + 1] == "opus"
    assert command[command.index("--allowedTools") + 1] == "Read,Bash(git *)"
    assert command[command.index("--disallowedTools") + 1] == "Bash(git push -f*)"
    assert command[command.index("--max-budget-usd") + 1] == "20.0"
    assert "--append-system-prompt-file" not in command
    assert "--effort" not in command
    high = runner.claude_command(runner.load_task("demo", make_task(tmp_path / "e", "effort: high\n")))
    assert high[high.index("--effort") + 1] == "high"
    assert runner.claude_command(task, 0.1)[runner.claude_command(task, 0.1).index(
        "--max-budget-usd") + 1] == "0.1"


# --- parallel items and per-item worktrees ------------------------------------

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


def test_parallel_parses_and_defaults_to_one(tmp_path):
    assert runner.load_task("demo", make_task(tmp_path / "one")).parallel == 1
    assert runner.load_task("demo", make_task(tmp_path / "four", "parallel: 4\n")).parallel == 4
    with pytest.raises(runner.TaskError, match="parallel"):
        runner.load_task("demo", make_task(tmp_path / "zero", "parallel: 0\n"))


def test_items_run_side_by_side_and_outcomes_keep_order(tmp_path, monkeypatch):
    task = runner.load_task("demo", make_task(tmp_path, "parallel: 3\n"))
    started = threading.Barrier(3, timeout=5)
    seen_cwds = []

    def fake_claude(command, prompt, timeout, env, cwd=None):
        seen_cwds.append(cwd)
        started.wait()  # only passes when all three sessions are running at once
        return 0, json.dumps({"result": prompt, "num_turns": 1}), ""

    monkeypatch.setattr(runner, "run_claude", fake_claude)
    monkeypatch.setattr(runner, "add_worktree", lambda key: tmp_path / f"wt-{key}")
    monkeypatch.setattr(runner, "remove_worktree", lambda path: None)
    items = [{"id": "a", "thing": "x"}, {"id": "b", "thing": "y"}, {"id": "c", "thing": "z"}]
    report = tmp_path / "report"
    report.mkdir()
    outcomes = runner.run_items(task, items, {}, report)
    assert [o["id"] for o in outcomes] == ["a", "b", "c"]
    assert all(o["status"] == "ok" for o in outcomes)
    assert sorted(seen_cwds) == [tmp_path / "wt-a", tmp_path / "wt-b", tmp_path / "wt-c"]
    assert json.loads((report / "b.json").read_text())["result"]["result"] == "Do y."


def test_a_failed_item_still_removes_its_worktree(tmp_path, monkeypatch):
    task = runner.load_task("demo", make_task(tmp_path))
    removed = []
    monkeypatch.setattr(runner, "add_worktree", lambda key: tmp_path / "wt")
    monkeypatch.setattr(runner, "remove_worktree", removed.append)
    monkeypatch.setattr(runner, "run_claude", lambda *a, **k: (1, "not json", "boom"))
    (tmp_path / "report").mkdir()
    outcome = runner.run_item(task, {"thing": "x"}, "0", {}, tmp_path / "report")
    assert outcome["status"] == "error" and removed == [tmp_path / "wt"]


def test_dry_run_tells_the_select_hook(tmp_path, monkeypatch, capsys):
    hook = "import json, os; print(json.dumps([{'id': os.environ.get('CLAUDE_TASK_DRY_RUN', 'unset')}]))\n"
    make_task(tmp_path, "select: pick_items.py\n")
    (tmp_path / "demo" / "pick_items.py").write_text(hook)
    load = runner.load_task
    monkeypatch.setattr(runner, "load_task", lambda name: load(name, tmp_path))
    monkeypatch.setattr(runner, "verify_token", lambda task, env, report_dir: "")
    monkeypatch.setattr(runner, "run_items", lambda *a: pytest.fail("dry run must not start items"))
    assert runner.main(["demo", "--dry-run"]) == 0
    assert '"id": "1"' in capsys.readouterr().out


@needs_git
def test_worktree_is_a_copy_of_head_and_is_removed(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    git = lambda *args: subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)  # noqa: E731
    git("init", "-q", "-b", "main")
    git("-c", "user.name=t", "-c", "user.email=t@x", "commit", "-q", "--allow-empty", "-m", "one")
    (repo / "note.txt").write_text("hello")
    git("add", "note.txt")
    git("-c", "user.name=t", "-c", "user.email=t@x", "commit", "-q", "-m", "two")
    monkeypatch.chdir(repo)
    worktree = runner.add_worktree("7")
    assert (worktree / "note.txt").read_text() == "hello"
    assert "7" in worktree.parent.name
    runner.remove_worktree(worktree)
    assert not worktree.parent.exists()
    listed = subprocess.run(["git", "worktree", "list"], cwd=repo, capture_output=True, text=True).stdout
    assert str(worktree) not in listed
