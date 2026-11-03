import base64
import plistlib
import subprocess
import xml.etree.ElementTree as ET

import pytest

from pdt import service_backends
from pdt.service_backends import BACKENDS, BackendError, task_xml


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
    monkeypatch.setattr(service_backends, "is_root", lambda: False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr(service_backends.Path, "home", lambda: tmp_path)
    found_answers = found, answers
    return found_answers


def programs(found):
    return [call["args"] for call in found]


def decoded(call) -> str:
    return base64.b64decode(call["args"][call["args"].index("-EncodedCommand") + 1]).decode("utf-16-le")


NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}


def test_the_parser_names_the_same_levels_and_backends_as_the_tables():
    from pdt import cli, service_commands
    assert cli.LEVELS == service_commands.LEVELS
    assert cli.BACKENDS == tuple(BACKENDS)


def test_the_table_has_one_entry_per_manager_with_each_action():
    assert list(BACKENDS) == ["windows", "systemd", "launchd", "cron"]
    for backend in BACKENDS.values():
        assert callable(backend.install) and callable(backend.remove)
        assert callable(backend.running) and callable(backend.available)


def test_the_windows_task_starts_at_boot_as_system_with_no_limit_and_restarts():
    command = [r"C:\Program Files\pdt\python.exe", "-m", "pdt.cli", "service", "run"]
    text = task_xml(command, service_backends.Path(r"C:\ProgramData\pdt\service"))
    root = ET.fromstring(text.replace('encoding="UTF-16"', ""))
    assert root.find("t:Triggers/t:BootTrigger/t:Enabled", NS).text == "true"
    assert root.find("t:Principals/t:Principal/t:UserId", NS).text == "S-1-5-18"
    assert root.find("t:Principals/t:Principal/t:RunLevel", NS).text == "HighestAvailable"
    assert root.find("t:Settings/t:ExecutionTimeLimit", NS).text == "PT0S"
    assert root.find("t:Settings/t:RestartOnFailure/t:Interval", NS).text == "PT1M"
    assert root.find("t:Settings/t:MultipleInstancesPolicy", NS).text == "IgnoreNew"
    assert root.find("t:Settings/t:DisallowStartIfOnBatteries", NS).text == "false"
    assert root.find("t:Actions/t:Exec/t:Command", NS).text == command[0]
    assert root.find("t:Actions/t:Exec/t:Arguments", NS).text == "-m pdt.cli service run"


def test_the_windows_install_registers_and_starts_the_task_with_no_pywin32(calls, monkeypatch):
    found, _answers = calls
    monkeypatch.setattr(service_backends.shutil, "which", lambda name: name)
    BACKENDS["windows"].install(service_backends.Path(r"C:\data"))
    script = decoded(found[0])
    assert "Register-ScheduledTask -TaskName 'pdt-service' -Xml $xml -Force" in script
    assert "Start-ScheduledTask -TaskName 'pdt-service'" in script
    assert "win32" not in script


def test_the_windows_remove_stops_and_unregisters(calls, monkeypatch):
    found, _answers = calls
    monkeypatch.setattr(service_backends.shutil, "which", lambda name: name)
    BACKENDS["windows"].remove()
    script = decoded(found[0])
    assert "Stop-ScheduledTask" in script and "Unregister-ScheduledTask" in script


@pytest.mark.parametrize("state, running", [("Running\r\n", True), ("Ready\r\n", False), ("", False)])
def test_the_windows_task_state_gives_running(calls, monkeypatch, state, running):
    _found, answers = calls
    monkeypatch.setattr(service_backends.shutil, "which", lambda name: name)
    answers[("powershell.exe",)] = subprocess.CompletedProcess([], 0, stdout=state, stderr="")
    assert BACKENDS["windows"].running() is running


def test_a_windows_failure_is_a_backend_error(calls, monkeypatch):
    _found, answers = calls
    monkeypatch.setattr(service_backends.shutil, "which", lambda name: name)
    answers[("powershell.exe",)] = subprocess.CompletedProcess([], 1, stdout="", stderr="denied")
    with pytest.raises(BackendError, match="denied"):
        BACKENDS["windows"].install(service_backends.Path("x"))


def test_systemd_writes_a_user_unit_and_enables_it(calls, tmp_path, monkeypatch):
    found, _answers = calls
    monkeypatch.setenv("PDT_INSTALLATION_DIR", "/data/pdt")
    BACKENDS["systemd"].install(tmp_path)
    unit = (tmp_path / ".config/systemd/user/pdt-service.service").read_text()
    assert "service run" in unit and "Restart=always" in unit
    assert "Environment=PDT_INSTALLATION_DIR=/data/pdt" in unit
    assert "WantedBy=default.target" in unit
    assert ["systemctl", "--user", "enable", "--now", "pdt-service.service"] in programs(found)
    assert ["loginctl", "enable-linger"] in programs(found)


def test_systemd_on_root_writes_a_machine_unit(calls, tmp_path, monkeypatch):
    found, _answers = calls
    monkeypatch.setattr(service_backends, "is_root", lambda: True)
    written = []
    monkeypatch.setattr(service_backends.Path, "write_text",
                        lambda self, text, encoding=None: written.append((self, text)))
    monkeypatch.setattr(service_backends.Path, "mkdir", lambda self, parents=False, exist_ok=False: None)
    BACKENDS["systemd"].install(tmp_path)
    assert str(written[0][0]) == "/etc/systemd/system/pdt-service.service"
    assert "WantedBy=multi-user.target" in written[0][1]
    assert ["systemctl", "enable", "--now", "pdt-service.service"] in programs(found)


def test_systemd_remove_and_running(calls, tmp_path):
    found, answers = calls
    BACKENDS["systemd"].install(tmp_path)
    answers[("systemctl", "--user", "is-active")] = subprocess.CompletedProcess([], 3, "", "")
    assert BACKENDS["systemd"].running() is False
    BACKENDS["systemd"].remove()
    assert not (tmp_path / ".config/systemd/user/pdt-service.service").exists()
    assert ["systemctl", "--user", "disable", "--now", "pdt-service.service"] in programs(found)


def test_launchd_writes_an_agent_and_bootstraps_it(calls, tmp_path, monkeypatch):
    found, _answers = calls
    monkeypatch.setattr(service_backends.os, "getuid", lambda: 501)
    BACKENDS["launchd"].install(tmp_path)
    path = tmp_path / "Library/LaunchAgents/com.autoidm.pdt-service.plist"
    plist = plistlib.loads(path.read_bytes())
    assert plist["ProgramArguments"][-2:] == ["service", "run"]
    assert plist["RunAtLoad"] is True and plist["KeepAlive"] is True
    assert ["launchctl", "bootstrap", "gui/501", str(path)] in programs(found)
    assert BACKENDS["launchd"].running() is True
    BACKENDS["launchd"].remove()
    assert not path.exists()
    assert ["launchctl", "bootout", "gui/501/com.autoidm.pdt-service"] in programs(found)


def test_cron_adds_one_tick_each_minute_and_keeps_other_lines(calls, tmp_path):
    found, answers = calls
    answers[("crontab", "-l")] = subprocess.CompletedProcess(
        [], 0, stdout="0 1 * * * backup\n* * * * * old service tick # pdt-service\n", stderr="")
    BACKENDS["cron"].install(tmp_path)
    written = next(call["input"] for call in found if call["args"] == ["crontab", "-"])
    lines = written.splitlines()
    assert lines[0] == "0 1 * * * backup"
    assert len(lines) == 2
    assert lines[1].startswith("* * * * * ")
    assert "pdt.cli service tick" in lines[1] and lines[1].endswith("# pdt-service")


def test_cron_remove_keeps_other_lines_and_running_reads_the_line(calls):
    found, answers = calls
    answers[("crontab", "-l")] = subprocess.CompletedProcess(
        [], 0, stdout="0 1 * * * backup\n* * * * * x # pdt-service\n", stderr="")
    assert BACKENDS["cron"].running() is True
    BACKENDS["cron"].remove()
    written = next(call["input"] for call in found if call["args"] == ["crontab", "-"])
    assert written == "0 1 * * * backup\n"


def test_cron_with_no_crontab_yet_adds_the_line(calls, tmp_path):
    found, answers = calls
    answers[("crontab", "-l")] = subprocess.CompletedProcess([], 1, stdout="", stderr="no crontab")
    BACKENDS["cron"].install(tmp_path)
    written = next(call["input"] for call in found if call["args"] == ["crontab", "-"])
    assert written.count("\n") == 1


def test_choose_takes_the_one_asked_for_and_else_the_first_here(monkeypatch):
    def only(*names):
        return {name: service_backends.Backend(lambda n=name: n in names, None, None, None)
                for name in ("windows", "systemd", "launchd", "cron")}

    monkeypatch.setattr(service_backends, "BACKENDS", only("launchd", "cron"))
    assert service_backends.choose(None) == "launchd"
    assert service_backends.choose("cron") == "cron"
    with pytest.raises(BackendError, match="systemd does not exist"):
        service_backends.choose("systemd")
    monkeypatch.setattr(service_backends, "BACKENDS", only("cron"))
    assert service_backends.choose(None) == "cron"
    monkeypatch.setattr(service_backends, "BACKENDS", only())
    with pytest.raises(BackendError, match="no Task Scheduler"):
        service_backends.choose(None)


def test_install_writes_settings_and_uses_the_table(tmp_path, monkeypatch, capsys):
    from pdt import service, service_runner
    from pdt.utils import autoidm
    monkeypatch.setenv(autoidm.DIR_ENV, str(tmp_path / "data"))
    folder = autoidm.folder()
    folder.mkdir(parents=True)
    (folder / "cus_7a1c09e4b2.json").write_text('{"command_keys": [{"kid": "k", "pem": "p"}]}')
    installed = []
    monkeypatch.setitem(service_backends.BACKENDS, "cron", service_backends.Backend(
        lambda: True, installed.append, lambda: installed.append("removed"), lambda: True))
    monkeypatch.chdir(tmp_path)
    assert service.install("view", str(tmp_path / "projects"), "cron") == 0
    settings = service.read_settings()
    assert settings["level"] == "view"
    assert settings["registrations"] == ["cus_7a1c09e4b2"]
    assert settings["projects"] == str((tmp_path / "projects").resolve())
    assert installed == [service_runner.service_folder()]
    assert service.install(None, None, "cron") == 0
    assert service.read_settings()["level"] == "view"
    assert service.remove() == 0
    assert installed[-1] == "removed"
    assert service.read_settings() is None


def test_install_with_no_registration_asks_for_one(tmp_path, monkeypatch, capsys):
    from pdt import service
    from pdt.utils import autoidm
    monkeypatch.setenv(autoidm.DIR_ENV, str(tmp_path / "data"))
    assert service.install(None, None, "cron") == 1
    assert "pdt register" in capsys.readouterr().out
