import json
from datetime import datetime, timezone

import pytest

from conftest import add_app
from pdt import config, deploy_windows


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.delenv("PDT_PROJECT", raising=False)
    (tmp_path / "pdt.yml").write_text("platform:\n  provider: windows\n")
    monkeypatch.setenv("ProgramData", str(tmp_path / "ProgramData"))
    monkeypatch.chdir(tmp_path)
    return tmp_path


def windows_app(project, monkeypatch):
    add_app(project, "my-report", "schedule: hourly\ntimezone: local\n")
    monkeypatch.setattr(deploy_windows, "_preflight",
                        lambda require_uv=True: ("powershell.exe", "uv.exe"))
    monkeypatch.setattr(deploy_windows, "_task_state", lambda powershell, name: "absent")
    monkeypatch.setattr(deploy_windows, "_deploying_user", lambda: r"PC\jon")
    return config.merged_app("my-report")


def test_deploy_plans_the_storage_folder(project, monkeypatch, capsys):
    app = windows_app(project, monkeypatch)
    assert deploy_windows.deploy(app, assume_yes=False) == 1
    folder = project / "ProgramData" / "pdt" / "my-report" / "storage"
    out = capsys.readouterr().out
    assert f"use folder {folder} for the app's files (kept after destroy)" in out


def test_destroy_reports_the_kept_folder(project, monkeypatch, capsys):
    app = windows_app(project, monkeypatch)
    folder = project / "ProgramData" / "pdt" / "my-report" / "storage"
    folder.mkdir(parents=True)
    (folder / "a.csv").write_text("a")
    assert deploy_windows.destroy(app, assume_yes=True) == 0
    out = capsys.readouterr().out
    assert f"kept: folder {folder} (1 files)" in out


def test_storage_ls_lists_the_folder_contents(project, monkeypatch, capsys):
    app = windows_app(project, monkeypatch)
    folder = project / "ProgramData" / "pdt" / "my-report" / "storage"
    folder.mkdir(parents=True)
    (folder / "a.csv").write_text("a")
    (folder / "b.csv").write_text("b")
    assert deploy_windows.storage(app, ["ls"], False) == 0
    out = capsys.readouterr().out
    assert "a.csv" in out
    assert "b.csv" in out


def destroy_with_task(project, monkeypatch, scripts):
    app = windows_app(project, monkeypatch)
    monkeypatch.setattr(deploy_windows, "_task_state", lambda powershell, name: "managed")
    monkeypatch.setattr(deploy_windows, "_run",
                        lambda powershell, script, **kw: scripts.append(script) or True)
    return app


def test_destroy_plans_the_task_the_logs_and_the_kept_storage(project, monkeypatch, capsys):
    scripts = []
    app = destroy_with_task(project, monkeypatch, scripts)
    folder = project / "ProgramData" / "pdt" / "my-report"
    (folder / "logs").mkdir(parents=True)
    (folder / "logs" / "20260923T100000Z.log").write_text("pdt: exit 0\n")
    (folder / "storage").mkdir()
    (folder / "storage" / "a.csv").write_text("a")

    assert deploy_windows.destroy(app, assume_yes=True) == 0

    out = capsys.readouterr().out
    assert "delete Windows scheduled task pdt-my-report" in out
    assert f"delete run logs folder {folder / 'logs'}" in out
    assert f"keep folder {folder / 'storage'} (the app's files)" in out
    assert f"removed: run logs folder {folder / 'logs'}" in out
    assert f"kept: folder {folder / 'storage'} (1 files)" in out
    (script,) = scripts
    assert script.index("Unregister-ScheduledTask") < script.index(
        f"Remove-Item -Recurse -Force -Path '{folder / 'logs'}'")
    assert str(folder / "storage") not in script


def test_destroy_without_a_logs_folder_deletes_only_the_task(project, monkeypatch, capsys):
    scripts = []
    app = destroy_with_task(project, monkeypatch, scripts)

    assert deploy_windows.destroy(app, assume_yes=True) == 0

    out = capsys.readouterr().out
    assert "delete run logs folder" not in out
    assert "removed: run logs folder" not in out
    (script,) = scripts
    assert "Remove-Item" not in script


def test_destroy_removes_a_leftover_logs_folder_when_the_task_is_gone(
        project, monkeypatch, capsys):
    scripts = []
    app = destroy_with_task(project, monkeypatch, scripts)
    monkeypatch.setattr(deploy_windows, "_task_state", lambda powershell, name: "absent")
    logs = project / "ProgramData" / "pdt" / "my-report" / "logs"
    logs.mkdir(parents=True)

    assert deploy_windows.destroy(app, assume_yes=True) == 0

    out = capsys.readouterr().out
    assert "delete Windows scheduled task" not in out
    assert f"removed: run logs folder {logs}" in out
    (script,) = scripts
    assert "Unregister-ScheduledTask" not in script


def test_destroy_warns_when_a_run_holds_the_state_lock(project, monkeypatch, capsys):
    scripts = []
    app = destroy_with_task(project, monkeypatch, scripts)
    state = project / "ProgramData" / "pdt" / "my-report" / "storage" / "state"
    state.mkdir(parents=True)
    started = datetime.now(timezone.utc).isoformat()
    (state / "lock").write_text(json.dumps({"run": "abcd1234", "started": started}))

    assert deploy_windows.destroy(app, assume_yes=True) == 0

    out = capsys.readouterr().out
    assert f"run abcd1234 of my-report started at {started} still holds the state" in out
    assert out.index("still holds the state") < out.index("Removed Windows task")


def test_destroy_without_storage_neither_warns_nor_keeps(project, monkeypatch, capsys):
    scripts = []
    destroy_with_task(project, monkeypatch, scripts)
    add_app(project, "my-report", "schedule: hourly\ntimezone: local\nstorage: false\n")
    app = config.merged_app("my-report")

    assert deploy_windows.destroy(app, assume_yes=True) == 0

    out = capsys.readouterr().out
    assert "kept:" not in out
    assert "keep folder" not in out
