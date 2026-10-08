"""The wrapper that runs a PowerShell app: `src/pdt/run_powershell.py`."""

import shutil
import subprocess
from pathlib import Path, PurePosixPath

import pytest

from conftest import add_app
from pdt import run_powershell


@pytest.fixture
def wrapper(monkeypatch):
    monkeypatch.setattr(run_powershell, "ensure_pwsh", lambda: "/fake/pwsh")
    monkeypatch.setattr(run_powershell.powershell, "extract", lambda folder: {"files": [
        {"file": path.name, "localInvocations": []}
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
