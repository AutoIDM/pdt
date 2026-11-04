import plistlib
import subprocess
import xml.etree.ElementTree as ET

import pytest

from pdt import service_backends
from pdt.service_backends import BACKENDS, BackendError, task_xml

PYTHON = "/var/lib/pdt/versions/0.1.14/bin/python"


@pytest.fixture
def calls(monkeypatch, tmp_path):
    """Record each program a backend starts, and answer it with `answers` by its first words."""
    found = []
    answers = {}

    def run(args, **kwargs):
        found.append({"args": args, "input": kwargs.get("input")})
        for words, answer in answers.items():
            if tuple(args[:len(words)]) == words:
                return answer
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(service_backends, "SYSTEMD_PATH", tmp_path / "pdt-service.service")
    monkeypatch.setattr(service_backends, "LAUNCHD_PATH", tmp_path / "com.autoidm.pdt-service.plist")
    return found, answers


def programs(found):
    return [call["args"] for call in found]


NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}


def test_the_parser_names_the_same_backends_as_the_table():
    from pdt import cli
    assert cli.BACKENDS == tuple(BACKENDS)


def test_the_windows_task_starts_at_boot_and_each_5_minutes_as_system_with_no_limit():
    command = [r"C:\ProgramData\pdt\versions\0.1.14\Scripts\python.exe", "-m", "pdt.cli",
               "service", "run"]
    text = task_xml(command, service_backends.Path(r"C:\ProgramData\pdt\service"))
    root = ET.fromstring(text.replace('encoding="UTF-16"', ""))
    assert root.find("t:Triggers/t:BootTrigger/t:Enabled", NS).text == "true"
    assert root.find("t:Triggers/t:TimeTrigger/t:Repetition/t:Interval", NS).text == "PT5M"
    assert root.find("t:Principals/t:Principal/t:UserId", NS).text == "S-1-5-18"
    assert root.find("t:Settings/t:ExecutionTimeLimit", NS).text == "PT0S"
    assert root.find("t:Settings/t:Priority", NS).text == "5"
    assert root.find("t:Settings/t:MultipleInstancesPolicy", NS).text == "IgnoreNew"
    assert root.find("t:Actions/t:Exec/t:Command", NS).text == command[0]
    assert root.find("t:Actions/t:Exec/t:Arguments", NS).text == "-m pdt.cli service run"


def test_the_windows_task_comes_from_a_file_and_remove_ends_it(calls, tmp_path):
    found, answers = calls
    BACKENDS["windows"].define(PYTHON, tmp_path)
    assert programs(found) == [["schtasks", "/Create", "/TN", "pdt-service", "/XML",
                                str(tmp_path / "task.xml"), "/F"]]
    found.clear()
    BACKENDS["windows"].remove()
    assert [args[1] for args in programs(found)] == ["/Query", "/End", "/Delete"]
    found.clear()
    answers[("schtasks", "/Query")] = subprocess.CompletedProcess([], 1, stdout="", stderr="")
    BACKENDS["windows"].remove()
    assert [args[1] for args in programs(found)] == ["/Query"]


def test_a_failed_program_is_a_backend_error(calls, tmp_path):
    _found, answers = calls
    answers[("schtasks", "/Run")] = subprocess.CompletedProcess([], 1, stdout="", stderr="denied")
    with pytest.raises(BackendError, match="denied"):
        BACKENDS["windows"].start(PYTHON)


def test_systemd_writes_the_unit_with_the_restart_rule(calls, tmp_path):
    found, _answers = calls
    BACKENDS["systemd"].define(PYTHON, tmp_path)
    unit = service_backends.SYSTEMD_PATH.read_text()
    assert f"ExecStart={PYTHON} -m pdt.cli service run" in unit
    assert "Restart=always\nRestartSec=60" in unit
    BACKENDS["systemd"].start(PYTHON)
    assert programs(found)[-1] == ["systemctl", "enable", "--now", "pdt-service.service"]
    BACKENDS["systemd"].remove()
    assert not service_backends.SYSTEMD_PATH.exists()


def test_launchd_keeps_the_service_alive_as_a_daemon(calls, tmp_path):
    found, _answers = calls
    BACKENDS["launchd"].define(PYTHON, tmp_path)
    plist = plistlib.loads(service_backends.LAUNCHD_PATH.read_bytes())
    assert plist["KeepAlive"] is True and plist["RunAtLoad"] is True
    assert plist["ProgramArguments"] == [PYTHON, "-m", "pdt.cli", "service", "run"]
    BACKENDS["launchd"].start(PYTHON)
    assert programs(found)[-1] == ["launchctl", "bootstrap", "system",
                                   str(service_backends.LAUNCHD_PATH)]


def test_cron_starts_at_boot_and_each_5_minutes_and_keeps_other_lines(calls, monkeypatch):
    found, answers = calls
    answers[("crontab", "-l")] = subprocess.CompletedProcess(
        [], 0, stdout="0 1 * * * backup\n", stderr="")
    BACKENDS["cron"].define(PYTHON, service_backends.Path("."))
    table = found[-1]["input"]
    assert table.startswith("0 1 * * * backup\n")
    assert f"@reboot {PYTHON} -m pdt.cli service run" in table
    assert f"*/5 * * * * {PYTHON} -m pdt.cli service run" in table
    started = []
    monkeypatch.setattr(subprocess, "Popen", lambda args, **_kwargs: started.append(args))
    BACKENDS["cron"].start(PYTHON)
    assert started == [[PYTHON, "-m", "pdt.cli", "service", "run"]]
    answers[("crontab", "-l")] = subprocess.CompletedProcess([], 0, stdout=table, stderr="")
    BACKENDS["cron"].remove()
    assert found[-1]["input"] == "0 1 * * * backup\n"


def test_choose_takes_the_one_asked_for_and_else_the_first_here(monkeypatch):
    for name, backend in BACKENDS.items():
        monkeypatch.setitem(BACKENDS, name, service_backends.Backend(
            (lambda found=name: found in ("launchd", "cron")), backend.define, backend.start,
            backend.remove, backend.running))
    assert service_backends.choose(None) == "launchd"
    assert service_backends.choose("cron") == "cron"
    with pytest.raises(BackendError, match="systemd does not exist"):
        service_backends.choose("systemd")
