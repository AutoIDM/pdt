from datetime import datetime, timezone
from pathlib import Path

import pytest

from conftest import add_app
from pdt import config, deploy_windows, runs_cli


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.delenv("PDT_PROJECT", raising=False)
    (tmp_path / "pdt.yml").write_text("platform:\n  provider: windows\n")
    monkeypatch.setenv("ProgramData", str(tmp_path / "ProgramData"))
    monkeypatch.chdir(tmp_path)
    return tmp_path


def windows_app(project):
    add_app(project, "my-report", "schedule: hourly\ntimezone: local\n")
    return config.merged_app("my-report")


def test_task_action_runs_the_runner_through_uv(project):
    app = windows_app(project)
    description, xml = deploy_windows.task_xml(app, "uv.exe", False)
    assert description == "hourly at minute 00"
    runner = Path(deploy_windows.__file__).resolve().with_name("run_windows_task.py")
    folder = project / "ProgramData" / "pdt" / "my-report"
    assert f"<Command>{Path('uv.exe').resolve()}</Command>" in xml
    assert (f"<Arguments>run --script {runner} {app['dir']} {folder / 'logs'} "
            f"{(folder / 'storage').as_uri()}/</Arguments>") in xml
    assert f"<WorkingDirectory>{app['dir']}</WorkingDirectory>" in xml
    assert "powershell" not in xml.lower()


def test_task_stops_a_run_after_30_minutes_and_skips_an_overlapping_start(project):
    app = windows_app(project)
    _description, xml = deploy_windows.task_xml(app, "uv.exe", False)
    assert "<ExecutionTimeLimit>PT30M</ExecutionTimeLimit>" in xml
    assert "<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>" in xml


def test_task_arguments_quote_a_folder_with_a_space(project):
    app = windows_app(project)
    app["dir"] = str(project / "my report")
    arguments = deploy_windows.task_arguments(app)
    assert f'"{project / "my report"}"' in arguments
    assert arguments.startswith("run --script ")


def test_storage_url_is_the_file_uri_with_a_trailing_slash(project):
    windows_app(project)
    folder = project / "ProgramData" / "pdt" / "my-report" / "storage"
    assert deploy_windows.storage_url("my-report") == folder.as_uri() + "/"
    assert deploy_windows.storage_url("my-report").startswith("file://")


def deploy_plan(project, monkeypatch, on_machine_path: bool) -> list[str]:
    app = windows_app(project)
    shown = []
    monkeypatch.setattr(deploy_windows, "_preflight", lambda: ("powershell.exe", "uv.exe"))
    monkeypatch.setattr(deploy_windows, "_task_state", lambda powershell, name: "absent")
    monkeypatch.setattr(deploy_windows, "_deploying_user", lambda: r"PC\jon")
    monkeypatch.setattr(deploy_windows, "uv_on_machine_path", lambda: on_machine_path)
    monkeypatch.setattr(deploy_windows, "confirm",
                        lambda actions, assume_yes, cost: shown.extend(actions))
    assert deploy_windows.deploy(app, assume_yes=False) == 1
    return shown


def test_plan_names_the_system_path_when_uv_is_on_it(project, monkeypatch):
    assert "run uv from the system PATH" in deploy_plan(project, monkeypatch, True)
    _description, xml = deploy_windows.task_xml(windows_app(project), "uv.exe", True)
    assert "<Command>uv</Command>" in xml


def test_plan_names_the_saved_path_when_uv_is_not_on_the_system_path(project, monkeypatch):
    saved = project / "uv.exe"
    assert (f"run uv from {saved} (uv is not on the system PATH; "
            "a machine-wide install drops the path from the task)"
            ) in deploy_plan(project, monkeypatch, False)
    _description, xml = deploy_windows.task_xml(windows_app(project), "uv.exe", False)
    assert f"<Command>{saved}</Command>" in xml


def test_uv_is_never_on_the_machine_path_off_windows():
    assert deploy_windows.uv_on_machine_path() is False


def test_list_runs_reads_a_finished_and_an_unfinished_file(project, monkeypatch):
    windows_app(project)
    folder = project / "ProgramData" / "pdt" / "my-report" / "logs"
    folder.mkdir(parents=True)
    (folder / "20260923T090000Z.log").write_text("10:00:00 INFO   starting\npdt: exit 0\n")
    (folder / "20260923T100000Z.log").write_text("10:00:00 INFO   starting\n")
    monkeypatch.setattr(deploy_windows.shutil, "which", lambda name: "powershell.exe")
    monkeypatch.setattr(deploy_windows, "_task_running", lambda powershell, name: True)

    found = deploy_windows.list_runs("my-report")

    assert [run.id for run in found] == ["20260923T100000Z", "20260923T090000Z"]
    running, finished = found
    assert running.started == datetime(2026, 9, 23, 10, 0, 0, tzinfo=timezone.utc)
    assert running.status == "running"
    assert running.ended is None
    assert running.exit_code is None
    assert finished.status == "succeeded"
    assert finished.exit_code == 0
    assert finished.ended is not None


def test_list_runs_without_a_running_task_is_failed(project, monkeypatch):
    windows_app(project)
    folder = project / "ProgramData" / "pdt" / "my-report" / "logs"
    folder.mkdir(parents=True)
    (folder / "20260923T100000Z.log").write_text("10:00:00 ERROR   boom\n")
    monkeypatch.setattr(deploy_windows.shutil, "which", lambda name: "powershell.exe")
    monkeypatch.setattr(deploy_windows, "_task_running", lambda powershell, name: False)

    found = deploy_windows.list_runs("my-report")

    assert found[0].status == "failed"


def test_list_runs_keeps_every_file(project):
    windows_app(project)
    folder = project / "ProgramData" / "pdt" / "my-report" / "logs"
    folder.mkdir(parents=True)
    for minute in range(60):
        (folder / f"20260923T10{minute:02d}00Z.log").write_text("pdt: exit 0\n")

    assert len(deploy_windows.list_runs("my-report")) == 60


def test_list_runs_with_no_folder_is_empty(project):
    windows_app(project)
    assert deploy_windows.list_runs("my-report") == []


def test_read_lines_parses_the_log_file(project):
    windows_app(project)
    folder = project / "ProgramData" / "pdt" / "my-report" / "logs"
    folder.mkdir(parents=True)
    (folder / "20260923T100000Z.log").write_text("10:00:00 INFO   starting\npdt: exit 0\n")
    run = runs_cli.Run("20260923T100000Z", datetime.now(timezone.utc), None, "succeeded")

    lines = deploy_windows.read_lines("my-report", run)

    assert lines == [
        runs_cli.Line(None, "INFO", "starting"),
        runs_cli.Line(None, "", "pdt: exit 0"),
    ]


def test_read_lines_with_no_file_is_empty(project):
    windows_app(project)
    run = runs_cli.Run("missing", datetime.now(timezone.utc), None, "failed")
    assert deploy_windows.read_lines("my-report", run) == []
