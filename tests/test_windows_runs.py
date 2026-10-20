from datetime import datetime, timezone
from pathlib import Path

import pytest

from conftest import add_app, plain
from pdt.deploy import DECLINED
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
    assert "powershell.exe" not in xml.lower()


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
                        lambda actions, assume_yes, cost: shown.extend(plain(actions)))
    assert deploy_windows.deploy(app, assume_yes=False) == DECLINED
    return shown


def test_plan_names_the_system_path_when_uv_is_on_it(project, monkeypatch):
    assert "run uv from the system PATH" in deploy_plan(project, monkeypatch, True)
    _description, xml = deploy_windows.task_xml(windows_app(project), "uv.exe", True)
    assert "<Command>uv</Command>" in xml


def test_plan_names_the_saved_path_when_uv_is_not_on_the_system_path(project, monkeypatch):
    saved = project / "uv.exe"
    assert f"run uv from {saved}" in deploy_plan(project, monkeypatch, False)
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


def test_list_runs_reads_an_exit_line_written_while_it_checks_the_task(project, monkeypatch):
    windows_app(project)
    folder = project / "ProgramData" / "pdt" / "my-report" / "logs"
    folder.mkdir(parents=True)
    log = folder / "20261020T132758Z.log"
    log.write_bytes(b"2026-10-20T13:29:24.100Z 06:29:24 INFO    Hello, world!\r\n")

    def task_ends(powershell, name):
        with log.open("ab") as out:
            out.write(b"2026-10-20T13:29:24.300Z pdt: exit 0\n")
        return False

    monkeypatch.setattr(deploy_windows.shutil, "which", lambda name: "powershell.exe")
    monkeypatch.setattr(deploy_windows, "_task_running", task_ends)

    (run,) = deploy_windows.list_runs("my-report")

    assert (run.status, run.exit_code) == ("succeeded", 0)
    assert run.ended is not None


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

    lines = list(deploy_windows.read_lines("my-report", run))

    assert lines == [
        runs_cli.Line(None, "INFO", "starting"),
        runs_cli.Line(None, "", "pdt: exit 0"),
    ]


def test_read_lines_with_no_file_is_empty(project):
    windows_app(project)
    run = runs_cli.Run("missing", datetime.now(timezone.utc), None, "failed")
    assert list(deploy_windows.read_lines("my-report", run)) == []
    assert list(deploy_windows.read_lines("my-report", run, follow=True)) == []


def test_read_lines_takes_each_line_time_from_the_runner(project):
    windows_app(project)
    folder = project / "ProgramData" / "pdt" / "my-report" / "logs"
    folder.mkdir(parents=True)
    (folder / "20260923T100000Z.log").write_bytes(
        b"2026-09-23T10:00:01.250Z \xef\xbb\xbf10:00:01 INFO     starting\n"
        b"2026-09-23T10:00:02.000Z pdt: exit 0\n")
    run = runs_cli.Run("20260923T100000Z", datetime.now(timezone.utc), None, "succeeded")

    assert list(deploy_windows.read_lines("my-report", run)) == [
        runs_cli.Line(datetime(2026, 9, 23, 10, 0, 1, 250000, tzinfo=timezone.utc), "INFO",
                      "starting"),
        runs_cli.Line(datetime(2026, 9, 23, 10, 0, 2, tzinfo=timezone.utc), "", "pdt: exit 0"),
    ]


def test_follow_reads_each_line_the_runner_adds_until_the_exit_line(project, monkeypatch):
    windows_app(project)
    folder = project / "ProgramData" / "pdt" / "my-report" / "logs"
    folder.mkdir(parents=True)
    log = folder / "20260923T100000Z.log"
    log.write_bytes(b"2026-09-23T10:00:01.000Z first\n2026-09-23T10:00:02.000Z sec")
    later = [b"ond\n", b"", b"2026-09-23T10:00:03.000Z pdt: exit 0\n"]
    naps = []

    def runner_writes(seconds):
        naps.append(seconds)
        with log.open("ab") as file:
            file.write(later.pop(0))
    monkeypatch.setattr(deploy_windows.time, "sleep", runner_writes)
    run = runs_cli.Run("20260923T100000Z", datetime.now(timezone.utc), None, "running")

    lines = deploy_windows.read_lines("my-report", run, follow=True)

    assert [line.message for line in lines] == ["first", "second", "pdt: exit 0"]
    assert naps == [deploy_windows.FOLLOW_POLL_SECONDS] * 3


def test_follow_ends_when_the_log_stays_quiet(project, monkeypatch):
    windows_app(project)
    folder = project / "ProgramData" / "pdt" / "my-report" / "logs"
    folder.mkdir(parents=True)
    (folder / "20260923T100000Z.log").write_bytes(b"2026-09-23T10:00:01.000Z first\n")
    clock = [0.0]
    monkeypatch.setattr(deploy_windows.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(deploy_windows.time, "sleep", lambda seconds: clock.__setitem__(
        0, clock[0] + seconds))
    run = runs_cli.Run("20260923T100000Z", datetime.now(timezone.utc), None, "running")

    lines = list(deploy_windows.read_lines("my-report", run, follow=True))

    assert [line.message for line in lines] == ["first"]
    assert clock[0] == runs_cli.STREAM_QUIET


def test_list_runs_reads_only_the_end_of_each_log_and_asks_only_about_the_newest(
        project, monkeypatch):
    windows_app(project)
    folder = project / "ProgramData" / "pdt" / "my-report" / "logs"
    folder.mkdir(parents=True)
    (folder / "20260923T090000Z.log").write_text("x" * 10_000 + "\n10:00:00 ERROR boom\n")
    (folder / "20260923T100000Z.log").write_text("x" * 10_000 + "\npdt: exit 0\n")
    (folder / "20260923T110000Z.log").write_text("10:00:00 INFO   starting\n")
    asked = []
    monkeypatch.setattr(deploy_windows.shutil, "which", lambda name: "powershell.exe")
    monkeypatch.setattr(deploy_windows, "_task_running",
                        lambda powershell, name: asked.append(name) or True)
    reads = []
    real_read = deploy_windows.parse_line
    monkeypatch.setattr(deploy_windows, "parse_line",
                        lambda raw: reads.append(len(raw)) or real_read(raw))

    found = deploy_windows.list_runs("my-report")

    assert [run.status for run in found] == ["running", "succeeded", "failed"]
    assert asked == ["pdt-my-report"]
    assert max(reads) < deploy_windows.EXIT_LINE_BYTES


def test_list_runs_does_not_take_an_exit_marker_from_the_middle_of_a_line(project, monkeypatch):
    windows_app(project)
    folder = project / "ProgramData" / "pdt" / "my-report" / "logs"
    folder.mkdir(parents=True)
    line = "2026-09-23T10:00:01.000Z said " + "x" * 300 + " pdt: exit 0 " + "y" * 200 + "\n"
    (folder / "20260923T100000Z.log").write_text(line)
    monkeypatch.setattr(deploy_windows.shutil, "which", lambda name: "powershell.exe")
    monkeypatch.setattr(deploy_windows, "_task_running", lambda powershell, name: True)

    (run,) = deploy_windows.list_runs("my-report")

    assert (run.status, run.exit_code) == ("running", None)


def test_a_read_of_a_running_log_holds_back_a_line_the_runner_has_not_ended(project):
    windows_app(project)
    folder = project / "ProgramData" / "pdt" / "my-report" / "logs"
    folder.mkdir(parents=True)
    (folder / "20260923T100000Z.log").write_bytes(
        b"2026-09-23T10:00:01.000Z first\n2026-09-23T10:00:02.000Z sec")
    run = runs_cli.Run("20260923T100000Z", datetime.now(timezone.utc), None, "running")

    assert [line.message for line in deploy_windows.read_lines("my-report", run)] == ["first"]


def two_logs(project, monkeypatch):
    app = windows_app(project)
    folder = project / "ProgramData" / "pdt" / "my-report" / "logs"
    folder.mkdir(parents=True)
    (folder / "20260923T090000Z.log").write_text("10:00:00 INFO   starting\npdt: exit 3\n")
    (folder / "20260923T100000Z.log").write_text("10:00:00 INFO   starting\n")
    monkeypatch.setattr(deploy_windows.shutil, "which", lambda name: "powershell.exe")
    monkeypatch.setattr(deploy_windows, "_task_running", lambda powershell, name: True)
    return app, folder


def test_logs_names_the_file_it_reads(project, monkeypatch, capsys):
    app, folder = two_logs(project, monkeypatch)
    assert deploy_windows.logs(app, ["2"]) == 1
    assert f"reading {folder / '20260923T090000Z.log'}" in capsys.readouterr().out


def test_logs_list_prints_the_folder_once_and_one_row_per_file(project, monkeypatch, capsys):
    app, folder = two_logs(project, monkeypatch)
    assert deploy_windows.logs(app, ["1", "--list"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == f"Logs folder: {folder}"
    assert lines[1].split() == ["#", "Log", "File", "Started", "Exit", "Code", "Size"]
    assert lines[2].split()[:2] == ["1", "20260923T100000Z.log"]
    assert lines[2].split()[-3:] == ["running", "25", "B"]
    assert lines[3].split()[:2] == ["2", "20260923T090000Z.log"]
    assert lines[3].split()[-3:] == ["3", "37", "B"]

