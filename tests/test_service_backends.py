import plistlib
import subprocess

import pytest

from pdt import service_backends
from pdt.service_backends import BACKENDS, BackendError

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


def test_the_parser_names_the_same_backends_as_the_table():
    from pdt import cli
    assert cli.BACKENDS == tuple(BACKENDS)


def test_the_windows_service_is_automatic_as_localsystem_and_restarts_after_a_failure(calls,
                                                                                    monkeypatch):
    found, _answers = calls
    python = r"C:\ProgramData\pdt\versions\0.1.14\venv\Scripts\python.exe"
    monkeypatch.delenv("PDT_INSTALLATION_DIR", raising=False)
    BACKENDS["windows"].define(python, service_backends.Path(r"C:\ProgramData\pdt\service"))
    assert programs(found) == [
        ["sc.exe", "create", "pdt-service", "binPath=", f"{python} -m pdt.service_main", "start=",
         "auto", "obj=", "LocalSystem", "DisplayName=", "pdt service"],
        ["sc.exe", "description", "pdt-service",
         "Runs the commands of the AutoIDM portal. Managed by pdt."],
        ["sc.exe", "failure", "pdt-service", "reset=", "86400", "actions=",
         "restart/5000/restart/5000/restart/60000"],
        ["sc.exe", "failureflag", "pdt-service", "1"]]


def test_the_windows_service_gets_the_data_folder_of_the_installer(calls, monkeypatch):
    found, _answers = calls
    monkeypatch.setenv("PDT_INSTALLATION_DIR", r"C:\pdt-tests\data")
    BACKENDS["windows"].define(r"C:\Program Files\pdt\python.exe", service_backends.Path("."))
    assert programs(found)[0][4] == r'"C:\Program Files\pdt\python.exe" -m pdt.service_main'
    assert programs(found)[-1] == [
        "reg.exe", "add", r"HKLM\SYSTEM\CurrentControlSet\Services\pdt-service", "/v",
        "Environment", "/t", "REG_MULTI_SZ", "/d", r"PDT_INSTALLATION_DIR=C:\pdt-tests\data", "/f"]


def test_windows_remove_stops_and_deletes_the_service_and_the_task_of_an_earlier_pdt(calls):
    found, answers = calls
    BACKENDS["windows"].remove()
    assert programs(found) == [
        ["schtasks.exe", "/Query", "/TN", "pdt-service"],
        ["schtasks.exe", "/End", "/TN", "pdt-service"],
        ["schtasks.exe", "/Delete", "/TN", "pdt-service", "/F"],
        ["sc.exe", "query", "pdt-service"],
        ["net.exe", "stop", "pdt-service"],
        ["sc.exe", "delete", "pdt-service"]]
    found.clear()
    answers[("schtasks.exe", "/Query")] = subprocess.CompletedProcess([], 1, stdout="", stderr="")
    answers[("sc.exe", "query")] = subprocess.CompletedProcess(
        [], 1060, stdout="The specified service does not exist as an installed service.", stderr="")
    BACKENDS["windows"].remove()
    assert programs(found) == [["schtasks.exe", "/Query", "/TN", "pdt-service"],
                               ["sc.exe", "query", "pdt-service"]]


def test_windows_start_is_net_start_and_running_reads_the_scm_state(calls):
    found, answers = calls
    BACKENDS["windows"].start(PYTHON)
    assert programs(found) == [["net.exe", "start", "pdt-service"]]
    answers[("sc.exe", "query")] = subprocess.CompletedProcess(
        [], 0, stdout="SERVICE_NAME: pdt-service\n        STATE              : 4  RUNNING \n",
        stderr="")
    assert BACKENDS["windows"].running() is True
    answers[("sc.exe", "query")] = subprocess.CompletedProcess(
        [], 0, stdout="SERVICE_NAME: pdt-service\n        STATE              : 1  STOPPED \n",
        stderr="")
    assert BACKENDS["windows"].running() is False


def test_a_failed_program_is_a_backend_error(calls, tmp_path):
    _found, answers = calls
    answers[("net.exe", "start")] = subprocess.CompletedProcess([], 2, stdout="",
                                                                stderr="System error 5 has occurred.")
    with pytest.raises(BackendError, match="System error 5"):
        BACKENDS["windows"].start(PYTHON)


def test_systemd_writes_the_unit_with_the_restart_rule(calls, tmp_path):
    found, _answers = calls
    BACKENDS["systemd"].define(PYTHON, tmp_path)
    unit = service_backends.SYSTEMD_PATH.read_text()
    assert f"ExecStart={PYTHON} -m pdt.service_main" in unit
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
    assert plist["ProgramArguments"] == [PYTHON, "-m", "pdt.service_main"]
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
    assert f"@reboot {PYTHON} -m pdt.service_main" in table
    assert f"*/5 * * * * {PYTHON} -m pdt.service_main" in table
    started = []
    monkeypatch.setattr(subprocess, "Popen", lambda args, **_kwargs: started.append(args))
    BACKENDS["cron"].start(PYTHON)
    assert started == [[PYTHON, "-m", "pdt.service_main"]]
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
