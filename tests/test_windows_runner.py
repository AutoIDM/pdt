"""The runner a Windows scheduled task calls: `src/pdt/run_windows_task.py`."""

import io
import os
import re
import subprocess
import sys
import types
from datetime import datetime, timezone
from pathlib import Path

import pytest

from pdt import run_windows_task

RUNNER = Path(run_windows_task.__file__)
LIFECYCLE = str(RUNNER.with_name("lifecycle.py"))


@pytest.fixture
def folders(tmp_path):
    app_dir = tmp_path / "my-report"
    app_dir.mkdir()
    (app_dir / "run.py").write_text("")
    return app_dir, tmp_path / "logs", (tmp_path / "storage").as_uri() + "/"


def fake_popen(calls, output: bytes, code: int):
    def popen(command, **kwargs):
        calls.append((command, kwargs))
        return types.SimpleNamespace(stdout=io.BytesIO(output), wait=lambda: code)
    return popen


def only_log(logs: Path) -> Path:
    (log,) = list(logs.glob("*.log"))
    assert re.fullmatch(r"\d{8}T\d{6}Z\.log", log.name)
    return log


def untimed(log: Path) -> str:
    lines = log.read_text().splitlines(keepends=True)
    assert all(run_windows_task.LINE_TIME.match(line) for line in lines)
    return "".join(run_windows_task.LINE_TIME.sub("", line, count=1) for line in lines)


def test_runner_logs_both_streams_and_exits_with_the_child_code(folders, monkeypatch):
    app_dir, logs, url = folders
    calls = []
    monkeypatch.delenv("UV", raising=False)
    monkeypatch.setattr(run_windows_task.subprocess, "Popen", fake_popen(calls, b"hello\n", 3))

    assert run_windows_task.main([str(app_dir), str(logs), url]) == 3

    (command, kwargs), = calls
    assert command == ["uv", "run", "--script", LIFECYCLE, "--", "uv", "run", "--script", "run.py"]
    assert kwargs["cwd"] == str(app_dir)
    assert kwargs["stdout"] is subprocess.PIPE
    assert kwargs["stderr"] is subprocess.STDOUT
    assert untimed(only_log(logs)) == "hello\npdt: exit 3\n"


def test_runner_sets_the_storage_url_for_the_child(folders, monkeypatch):
    app_dir, logs, url = folders
    calls = []
    monkeypatch.setenv("UV", "/tools/uv.exe")
    monkeypatch.setattr(run_windows_task.subprocess, "Popen", fake_popen(calls, b"", 0))

    assert run_windows_task.main([str(app_dir), str(logs), url]) == 0

    (command, kwargs), = calls
    assert command[0] == "/tools/uv.exe"
    assert kwargs["env"]["PDT_STORAGE_URL"] == url
    assert kwargs["env"]["PYTHONIOENCODING"] == "utf-8"
    assert kwargs["env"]["PYTHONUNBUFFERED"] == "1"


def test_runner_runs_an_app_without_run_py_through_the_powershell_wrapper(folders, monkeypatch):
    app_dir, logs, url = folders
    (app_dir / "run.py").unlink()
    (app_dir / "report.ps1").write_text("")
    calls = []
    monkeypatch.setenv("UV", "/tools/uv.exe")
    monkeypatch.setattr(run_windows_task.subprocess, "Popen", fake_popen(calls, b"", 0))

    assert run_windows_task.main([str(app_dir), str(logs), url]) == 0

    (command, kwargs), = calls
    assert command == ["/tools/uv.exe", "run", "--script", LIFECYCLE, "--", "/tools/uv.exe", "run",
                       "--script", str(RUNNER.with_name("run_powershell.py")), str(app_dir)]
    assert kwargs["cwd"] == str(app_dir)


def test_runner_names_the_run_after_its_log(folders, monkeypatch):
    app_dir, logs, url = folders
    calls = []
    monkeypatch.setattr(run_windows_task.subprocess, "Popen", fake_popen(calls, b"", 0))

    assert run_windows_task.main([str(app_dir), str(logs), url]) == 0

    (_command, kwargs), = calls
    assert kwargs["env"]["PDT_RUN_ID"] == only_log(logs).stem


def test_runner_without_uv_logs_exit_127(folders, monkeypatch):
    app_dir, logs, url = folders

    def missing(command, **kwargs):
        raise FileNotFoundError("uv")
    monkeypatch.setattr(run_windows_task.subprocess, "Popen", missing)

    assert run_windows_task.main([str(app_dir), str(logs), url]) == 127
    text = untimed(only_log(logs))
    assert "could not be started" in text
    assert text.endswith("pdt: exit 127\n")


def test_runner_rejects_the_wrong_number_of_arguments(capsys):
    assert run_windows_task.main(["only-one"]) == 2
    assert "usage: run_windows_task.py" in capsys.readouterr().err


@pytest.mark.skipif(os.name == "nt", reason="the fake uv is a shell script")
def test_runner_process_exit_code_is_the_child_exit_code(folders, tmp_path):
    app_dir, logs, url = folders
    fake_uv = tmp_path / "uv"
    fake_uv.write_text("#!/bin/sh\necho \"args: $*\"\necho oops >&2\nexit 5\n")
    fake_uv.chmod(0o755)

    proc = subprocess.run([sys.executable, str(RUNNER), str(app_dir), str(logs), url],
                          env={**os.environ, "UV": str(fake_uv)}, capture_output=True)

    assert proc.returncode == 5
    assert proc.stdout == b"" and proc.stderr == b""
    assert untimed(only_log(logs)) == (f"args: run --script {LIFECYCLE} -- {fake_uv} run --script run.py"
                                          "\noops\npdt: exit 5\n")


@pytest.mark.parametrize(("age", "trigger"), [(0, "manual"), (600, None)])
def test_runner_marks_a_run_manual_when_pdt_left_a_fresh_marker(folders, monkeypatch, age, trigger):
    app_dir, logs, url = folders
    logs.mkdir()
    marker = logs / run_windows_task.MANUAL_START
    marker.touch()
    old = marker.stat().st_mtime - age
    os.utime(marker, (old, old))
    calls = []
    monkeypatch.delenv("PDT_TRIGGER", raising=False)
    monkeypatch.setattr(run_windows_task.subprocess, "Popen", fake_popen(calls, b"", 0))
    assert run_windows_task.main([str(app_dir), str(logs), url]) == 0
    assert calls[0][1]["env"].get("PDT_TRIGGER") == trigger
    assert not marker.exists()


def test_runner_puts_the_utc_time_before_each_line(folders, monkeypatch):
    app_dir, logs, url = folders
    monkeypatch.setattr(run_windows_task.subprocess, "Popen", fake_popen([], b"one\r\ntwo", 0))
    before = datetime.now(timezone.utc).replace(microsecond=0)

    run_windows_task.main([str(app_dir), str(logs), url])

    lines = only_log(logs).read_text().splitlines()
    times = [datetime.fromisoformat(run_windows_task.LINE_TIME.match(line).group(1))
             for line in lines]
    assert [line.split(" ", 1)[1] for line in lines] == ["one", "two", "pdt: exit 0"]
    assert all(before <= moment <= datetime.now(timezone.utc) for moment in times)


def test_runner_keeps_old_logs_and_runs_folders(folders, monkeypatch):
    app_dir, logs, url = folders
    runs = app_dir.parent / "storage" / "runs"
    runs.mkdir(parents=True)
    logs.mkdir()
    (logs / "20200101T000000Z.log").write_text("pdt: exit 0\n")
    (runs / "20200101T000000Z-a1" / "output").mkdir(parents=True)
    monkeypatch.setattr(run_windows_task.subprocess, "Popen", fake_popen([], b"", 0))

    run_windows_task.main([str(app_dir), str(logs), url])

    assert [path.name for path in runs.iterdir()] == ["20200101T000000Z-a1"]
    assert len(list(logs.glob("*.log"))) == 2


@pytest.mark.skipif(os.name == "nt", reason="the fake uv is a shell script")
def test_runner_ends_when_the_app_exits_though_a_background_process_holds_its_output(
        folders, tmp_path):
    app_dir, logs, url = folders
    fake_uv = tmp_path / "uv"
    fake_uv.write_text("#!/bin/sh\necho started\nprintf 'no newline'\nsleep 30 &\nexit 0\n")
    fake_uv.chmod(0o755)

    proc = subprocess.run([sys.executable, str(RUNNER), str(app_dir), str(logs), url],
                          env={**os.environ, "UV": str(fake_uv)}, timeout=20)

    assert proc.returncode == 0
    assert untimed(only_log(logs)) == "started\nno newline\npdt: exit 0\n"


@pytest.mark.parametrize(("code", "kind", "event"), [(0, 4, 1000), (3, 1, 1001)])
def test_runner_writes_the_result_to_the_application_log(folders, monkeypatch, code, kind, event):
    app_dir, logs, url = folders
    reported = []
    advapi = types.SimpleNamespace(
        RegisterEventSourceW=lambda server, source: reported.append(source) or 7,
        ReportEventW=lambda *args: reported.append(args[:4] + (list(args[7]),)),
        DeregisterEventSource=lambda source: reported.append(("closed", source)))
    monkeypatch.setattr(run_windows_task.subprocess, "Popen", fake_popen([], b"", code))
    monkeypatch.setattr("ctypes.WinDLL", lambda name: advapi, raising=False)

    assert run_windows_task.main([str(app_dir), str(logs), url]) == code

    log = only_log(logs)
    assert reported == [
        "pdt",
        (7, kind, 0, event, [f"pdt app my-report, run {log.stem}, ended with exit code "
                             f"{code}. Log: {log}"]),
        ("closed", 7)]
