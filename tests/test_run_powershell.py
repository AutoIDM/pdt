"""The wrapper that runs a PowerShell app: `src/pdt/run_powershell.py`."""

import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

import pytest

from conftest import add_app
from pdt import run_powershell


@pytest.fixture
def wrapper(monkeypatch):
    monkeypatch.setattr(run_powershell, "ensure_pwsh", lambda: "/fake/pwsh")
    monkeypatch.setattr(run_powershell.powershell, "extract", lambda folder: {"files": [
        {"file": path.name, "localInvocations": [],
         "envReads": [{"name": "PDT_STATE_DIR", "line": 1, "set": False}] if "PDT_STATE_DIR" in path.read_text() else []}
        for path in sorted(folder.iterdir()) if path.suffix == ".ps1"]})
    return run_powershell


@pytest.fixture
def app_dir(project, monkeypatch):
    folder = project / "my-report"
    folder.mkdir()
    (folder / "config.yml").write_text("schedule: daily\n")
    monkeypatch.chdir(folder)
    monkeypatch.delenv("PDT_STORAGE_URL", raising=False)
    return folder


def fake_pwsh(calls, codes):
    def run(command, **kwargs):
        calls.append((command, kwargs))
        script = command[-1].split("& '", 1)[1].split("'", 1)[0]
        output = Path(kwargs["env"]["PDT_OUTPUT_DIR"])
        (output / f"{Path(script).stem}.csv").write_text("a,b\n")
        return subprocess.CompletedProcess(command, codes[Path(script).name])
    return run


def stateful_pwsh(calls, codes):
    """A script appends its name to state's seen.txt; the call records what the file held."""
    def run(command, **kwargs):
        script = Path(command[-1].split("& '", 1)[1].split("'", 1)[0])
        seen = Path(kwargs["env"]["PDT_STATE_DIR"]) / "seen.txt"
        calls.append(seen.read_text() if seen.exists() else "")
        seen.write_text(calls[-1] + script.stem + "\n")
        return subprocess.CompletedProcess(command, codes[script.name])
    return run


def stored_state(app_dir):
    return app_dir.parent / ".pdt" / "storage" / "my-report" / "state"


def test_the_pwsh_command_quotes_the_path_and_keeps_the_exit_code(wrapper):
    command = wrapper.pwsh_command(PurePosixPath("/apps/it's here/report.ps1"))
    assert command == (
        "function Clear-Host {}; Set-Alias -Name cls -Value Clear-Host -Force; "
        "Set-Alias -Name clear -Value Clear-Host -Force; "
        "$ErrorActionPreference='Stop'; $PSNativeCommandUseErrorActionPreference=$true; "
        "try { & '/apps/it''s here/report.ps1' } catch { "
        "[Console]::Error.WriteLine(($_ | Out-String).TrimEnd()); "
        "[Console]::Error.WriteLine($_.ScriptStackTrace); exit 1 }; exit $LASTEXITCODE")


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="needs pwsh")
def test_the_pwsh_command_shadows_clear_host_aliases(tmp_path, wrapper):
    script = tmp_path / "report.ps1"
    script.write_text("Clear-Host\ncls\nclear\n")

    result = subprocess.run(
        [shutil.which("pwsh"), "-NoProfile", "-NonInteractive", "-Command",
         wrapper.pwsh_command(script)], capture_output=True, text=True)

    assert result.returncode == 0, result.stderr


def test_scripts_run_in_order_and_the_output_is_stored(app_dir, wrapper, monkeypatch, capsys):
    (app_dir / "b.ps1").write_text("")
    (app_dir / "a.ps1").write_text("")
    calls = []
    monkeypatch.setattr(wrapper.subprocess, "run", fake_pwsh(calls, {"a.ps1": 0, "b.ps1": 0}))

    assert wrapper.main([str(app_dir)]) == 0

    assert [command[:4] for command, _ in calls] == [
        ["/fake/pwsh", "-NoProfile", "-NonInteractive", "-Command"]] * 2
    assert [command[-1].split("& '")[1].split("'")[0] for command, _ in calls] == [
        str(app_dir / "a.ps1"), str(app_dir / "b.ps1")]
    assert all(kwargs["cwd"] == app_dir for _, kwargs in calls)
    (run,) = (app_dir.parent / ".pdt" / "storage" / "my-report" / "runs").iterdir()
    assert sorted(path.name for path in (run / "output").iterdir()) == ["_done", "a.csv", "b.csv"]
    out = capsys.readouterr().out
    assert "starting a.ps1" in out and "b.ps1 ended  exit_code=0" in out
    assert "uploaded 2 output file(s) to " in out


def test_each_script_gets_the_merged_config_section_as_json(app_dir, wrapper, monkeypatch):
    (app_dir / "a.ps1").write_text("")
    (app_dir / "config.yml").write_text("schedule: daily\nconfig:\n  days: 7\n  types: [A, B]\n")
    monkeypatch.setenv("PDT_MY_REPORT_DAYS", "30")
    calls = []
    monkeypatch.setattr(wrapper.subprocess, "run", fake_pwsh(calls, {"a.ps1": 0}))

    assert wrapper.main([str(app_dir)]) == 0

    (_, kwargs), = calls
    assert json.loads(kwargs["env"]["PDT_CONFIG_JSON"]) == {"days": "30", "types": ["A", "B"]}


def test_a_yaml_date_in_the_config_section_reaches_the_script_as_text(app_dir, wrapper, monkeypatch):
    (app_dir / "a.ps1").write_text("")
    (app_dir / "config.yml").write_text("schedule: daily\nconfig:\n  since: 2026-01-31\n")
    calls = []
    monkeypatch.setattr(wrapper.subprocess, "run", fake_pwsh(calls, {"a.ps1": 0}))

    assert wrapper.main([str(app_dir)]) == 0

    (_, kwargs), = calls
    assert json.loads(kwargs["env"]["PDT_CONFIG_JSON"]) == {"since": "2026-01-31"}


def test_the_from_scripts_run_py_passes_the_config_section_too(app_dir, monkeypatch):
    from pdt import scaffold

    (app_dir / "a.ps1").write_text("")
    (app_dir / "config.yml").write_text("schedule: daily\nconfig:\n  since: 2026-01-31\n  days: 7\n")
    run_py = app_dir / "run.py"
    run_py.write_text(scaffold.RUN_PY_TEXT)
    namespace = {"__file__": str(run_py), "__name__": "run_py"}
    exec(compile(scaffold.RUN_PY_TEXT, str(run_py), "exec"), namespace)
    monkeypatch.setitem(namespace, "ensure_pwsh", lambda: "/fake/pwsh")
    monkeypatch.setattr(namespace["powershell"], "extract", lambda folder: {"files": [
        {"file": "a.ps1", "localInvocations": [], "envReads": []}]})
    calls = []
    monkeypatch.setattr(namespace["subprocess"], "run", fake_pwsh(calls, {"a.ps1": 0}))

    assert namespace["main"]() == 0

    (_, kwargs), = calls
    assert json.loads(kwargs["env"]["PDT_CONFIG_JSON"]) == {"since": "2026-01-31", "days": 7}


def test_the_first_failure_stops_the_run_and_its_output_is_still_stored(
        app_dir, wrapper, monkeypatch):
    for name in ("first.ps1", "second.ps1", "third.ps1"):
        (app_dir / name).write_text("")
    (app_dir / "config.yml").write_text(
        "schedule: daily\nrun_scripts: [third.ps1, second.ps1, first.ps1]\n")
    calls = []
    monkeypatch.setattr(wrapper.subprocess, "run",
                        fake_pwsh(calls, {"third.ps1": 0, "second.ps1": 3, "first.ps1": 0}))

    assert wrapper.main([str(app_dir)]) == 3

    assert len(calls) == 2
    (run,) = (app_dir.parent / ".pdt" / "storage" / "my-report" / "runs").iterdir()
    assert sorted(path.name for path in (run / "output").iterdir()) == ["_done", "second.csv", "third.csv"]


def test_continue_on_error_runs_every_script_and_returns_the_first_failure(
        app_dir, wrapper, monkeypatch, capsys):
    for name in ("first.ps1", "second.ps1", "third.ps1"):
        (app_dir / name).write_text("")
    (app_dir / "config.yml").write_text(
        "schedule: daily\ncontinue_on_error: true\n")
    calls = []
    monkeypatch.setattr(wrapper.subprocess, "run",
                        fake_pwsh(calls, {"first.ps1": 3, "second.ps1": 4, "third.ps1": 0}))

    assert wrapper.main([str(app_dir)]) == 3

    assert len(calls) == 3
    out = capsys.readouterr().out
    assert "first.ps1 failed  exit_code=3" in out
    assert "second.ps1 failed  exit_code=4" in out


def test_run_ps1_is_the_only_script_that_runs(app_dir, wrapper, monkeypatch):
    for name in ("run.ps1", "report.ps1", "helper.ps1"):
        (app_dir / name).write_text("")
    calls = []
    monkeypatch.setattr(wrapper.subprocess, "run",
                        fake_pwsh(calls, {"run.ps1": 0, "report.ps1": 0, "helper.ps1": 0}))

    assert wrapper.main([str(app_dir)]) == 0

    assert len(calls) == 1
    assert str(app_dir / "run.ps1") in calls[0][0][-1]


def test_storage_false_keeps_nothing(app_dir, wrapper, monkeypatch):
    (app_dir / "a.ps1").write_text("")
    (app_dir / "config.yml").write_text("schedule: daily\nstorage: false\n")
    monkeypatch.setattr(wrapper.subprocess, "run", fake_pwsh([], {"a.ps1": 0}))

    assert wrapper.main([str(app_dir)]) == 0

    assert not (app_dir.parent / ".pdt").exists()


def test_a_python_app_is_not_a_powershell_app(project, wrapper):
    folder = add_app(project, "py-report")
    (folder / "extra.ps1").write_text("")
    from pdt import config
    assert config.powershell_scripts(folder) == []


def test_a_missing_required_env_var_stops_the_run_before_any_script(
        app_dir, wrapper, monkeypatch, capsys):
    (app_dir / "report.ps1").write_text("")
    (app_dir / "config.yml").write_text("schedule: daily\nenv:\n  required: [PDT_TEST_TENANT]\n")
    monkeypatch.delenv("PDT_TEST_TENANT", raising=False)
    monkeypatch.setattr(wrapper.subprocess, "run", lambda *a, **k: pytest.fail("a script ran"))

    with pytest.raises(SystemExit) as stop:
        wrapper.main([str(app_dir)])

    assert stop.value.code == 1
    out = capsys.readouterr().out
    assert "env vars missing: missing required env var PDT_TEST_TENANT." in out
    assert f"add each one as NAME=value to {app_dir.parent / '.env'}" in out


def test_a_script_that_reads_pdt_state_dir_starts_from_the_last_runs_state(app_dir, wrapper, monkeypatch, capsys):
    (app_dir / "a.ps1").write_text("$env:PDT_STATE_DIR")
    calls = []
    monkeypatch.setattr(wrapper.subprocess, "run", stateful_pwsh(calls, {"a.ps1": 0}))

    assert wrapper.main([str(app_dir)]) == 0
    assert wrapper.main([str(app_dir)]) == 0

    assert calls == ["", "a\n"]
    assert sorted(path.name for path in stored_state(app_dir).iterdir()) == ["seen.txt"]
    assert (stored_state(app_dir) / "seen.txt").read_text() == "a\na\n"
    assert "saved state to " in capsys.readouterr().out


def test_a_failed_script_leaves_state_as_the_last_good_run_left_it(app_dir, wrapper, monkeypatch, capsys):
    (app_dir / "a.ps1").write_text("$env:PDT_STATE_DIR")
    (app_dir / "b.ps1").write_text("")
    stored_state(app_dir).mkdir(parents=True)
    (stored_state(app_dir) / "seen.txt").write_text("before\n")
    calls = []
    monkeypatch.setattr(wrapper.subprocess, "run", stateful_pwsh(calls, {"a.ps1": 0, "b.ps1": 5}))

    assert wrapper.main([str(app_dir)]) == 5

    assert calls == ["before\n", "before\na\n"]
    assert sorted(path.name for path in stored_state(app_dir).iterdir()) == ["seen.txt"]
    assert (stored_state(app_dir) / "seen.txt").read_text() == "before\n"
    assert "state/ keeps the last good run's files" in capsys.readouterr().out


def test_a_run_that_another_run_holds_the_state_of_stops_before_any_script(
        app_dir, wrapper, monkeypatch, capsys):
    (app_dir / "a.ps1").write_text("$env:PDT_STATE_DIR")
    stored_state(app_dir).mkdir(parents=True)
    (stored_state(app_dir) / "lock").write_text(json.dumps(
        {"run": "other", "host": "elsewhere", "started": datetime.now(timezone.utc).isoformat()}))
    monkeypatch.setattr(wrapper.subprocess, "run", lambda *a, **k: pytest.fail("a script ran"))

    with pytest.raises(SystemExit) as stop:
        wrapper.main([str(app_dir)])

    assert stop.value.code == 1
    assert "another run of my-report started at" in capsys.readouterr().out


def test_scripts_that_do_not_read_pdt_state_dir_leave_state_alone(app_dir, wrapper, monkeypatch):
    (app_dir / "a.ps1").write_text("")
    calls = []
    monkeypatch.setattr(wrapper.subprocess, "run", stateful_pwsh(calls, {"a.ps1": 0}))

    assert wrapper.main([str(app_dir)]) == 0

    assert calls == [""]
    assert not stored_state(app_dir).exists()
