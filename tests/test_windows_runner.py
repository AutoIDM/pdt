"""The runner a Windows scheduled task calls: `src/pdt/run_windows_task.py`."""

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from pdt import run_windows_task

RUNNER = Path(run_windows_task.__file__)


@pytest.fixture
def folders(tmp_path):
    app_dir = tmp_path / "my-report"
    app_dir.mkdir()
    return app_dir, tmp_path / "logs", (tmp_path / "storage").as_uri() + "/"


def fake_run(calls, output: bytes, code: int):
    def run(command, **kwargs):
        calls.append((command, kwargs))
        kwargs["stdout"].write(output)
        return subprocess.CompletedProcess(command, code)
    return run


def only_log(logs: Path) -> Path:
    (log,) = list(logs.glob("*.log"))
    assert re.fullmatch(r"\d{8}T\d{6}Z\.log", log.name)
    return log


def test_runner_logs_both_streams_and_exits_with_the_child_code(folders, monkeypatch):
    app_dir, logs, url = folders
    calls = []
    monkeypatch.delenv("UV", raising=False)
    monkeypatch.setattr(run_windows_task.subprocess, "run", fake_run(calls, b"hello\n", 3))

    assert run_windows_task.main([str(app_dir), str(logs), url]) == 3

    (command, kwargs), = calls
    assert command == ["uv", "run", "--script", "run.py"]
    assert kwargs["cwd"] == str(app_dir)
    assert kwargs["stderr"] is subprocess.STDOUT
    assert only_log(logs).read_text() == "hello\npdt: exit 3\n"


def test_runner_sets_the_storage_url_for_the_child(folders, monkeypatch):
    app_dir, logs, url = folders
    calls = []
    monkeypatch.setenv("UV", "/tools/uv.exe")
    monkeypatch.setattr(run_windows_task.subprocess, "run", fake_run(calls, b"", 0))

    assert run_windows_task.main([str(app_dir), str(logs), url]) == 0

    (command, kwargs), = calls
    assert command[0] == "/tools/uv.exe"
    assert kwargs["env"]["PDT_STORAGE_URL"] == url
    assert kwargs["env"]["PYTHONIOENCODING"] == "utf-8"


def test_runner_names_the_run_after_its_log(folders, monkeypatch):
    app_dir, logs, url = folders
    calls = []
    monkeypatch.setattr(run_windows_task.subprocess, "run", fake_run(calls, b"", 0))

    assert run_windows_task.main([str(app_dir), str(logs), url]) == 0

    (_command, kwargs), = calls
    assert kwargs["env"]["PDT_RUN_ID"] == only_log(logs).stem


def test_runner_without_uv_logs_exit_127(folders, monkeypatch):
    app_dir, logs, url = folders

    def missing(command, **kwargs):
        raise FileNotFoundError("uv")
    monkeypatch.setattr(run_windows_task.subprocess, "run", missing)

    assert run_windows_task.main([str(app_dir), str(logs), url]) == 127
    text = only_log(logs).read_text()
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
    assert only_log(logs).read_text() == "args: run --script run.py\noops\npdt: exit 5\n"
