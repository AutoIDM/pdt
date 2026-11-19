import subprocess

import pytest

from pdt import service_backends
from pdt.service_backends import BACKENDS, BackendError

PYTHON = r"C:\ProgramData\pdt\versions\0.1.14\python\python.exe"


@pytest.fixture
def calls(monkeypatch, tmp_path):
    found = []
    answers = {}

    def run(args, **kwargs):
        found.append({"args": args, "input": kwargs.get("input")})
        for words, answer in answers.items():
            if tuple(args[:len(words)]) == words:
                return answer
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", run)
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




def test_the_service_runs_only_on_windows(monkeypatch):
    monkeypatch.setattr(service_backends.os, "name", "posix")
    with pytest.raises(BackendError, match="the pdt service runs only on Windows"):
        service_backends.choose(None)
    monkeypatch.setattr(service_backends.os, "name", "nt")
    assert service_backends.choose(None) == "windows"
