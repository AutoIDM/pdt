import subprocess

import pytest

from pdt import deploy_azure
from pdt import deploy_azure_container_apps


def test_internal_error_retries_with_the_same_arguments(monkeypatch, capsys):
    attempts = iter([
        subprocess.CompletedProcess([], 1, "", "ERROR: (InternalServerError) correlation ID: first"),
        subprocess.CompletedProcess([], 0, "", ""),
    ])
    calls = []
    sleeps = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return next(attempts)

    monkeypatch.setattr(deploy_azure.subprocess, "run", run)
    monkeypatch.setattr(deploy_azure.time, "sleep", sleeps.append)

    deploy_azure.run_quiet("containerapp", "job", "create",
                           "--name", "pdt-report", retry_internal=True)

    assert [call[0] for call in calls] == [calls[0][0], calls[0][0]]
    assert sleeps == [10]
    assert "correlation ID: first" in capsys.readouterr().out


def test_four_internal_errors_are_printed_and_fail(monkeypatch, capsys):
    attempts = iter([
        subprocess.CompletedProcess([], 1, "", f"ERROR: (InternalServerError) correlation ID: {name}")
        for name in ("one", "two", "three", "four")
    ])
    sleeps = []
    monkeypatch.setattr(deploy_azure.subprocess, "run", lambda *args, **kwargs: next(attempts))
    monkeypatch.setattr(deploy_azure.time, "sleep", sleeps.append)

    with pytest.raises(SystemExit):
        deploy_azure.run_quiet("containerapp", "job", "create", retry_internal=True)

    output = capsys.readouterr().out
    assert all(f"correlation ID: {name}" in output
               for name in ("one", "two", "three", "four"))
    assert sleeps == [10, 20, 40]


def test_permission_error_does_not_retry_with_internal_only(monkeypatch):
    calls = []
    monkeypatch.setattr(
        deploy_azure.subprocess, "run",
        lambda *args, **kwargs: calls.append(args) or subprocess.CompletedProcess([], 1, "", "Forbidden"))
    monkeypatch.setattr(deploy_azure.time, "sleep",
                        lambda seconds: pytest.fail(f"unexpected {seconds}s sleep"))

    with pytest.raises(SystemExit):
        deploy_azure.run_quiet("containerapp", "job", "update", retry_internal=True)

    assert len(calls) == 1


@pytest.mark.parametrize("message,retry_internal", [
    ("Bad Gateway", True),
    ("InternalServerError", False),
])
def test_server_error_does_not_retry_without_matching_opt_in(monkeypatch, message, retry_internal):
    calls = []
    monkeypatch.setattr(
        deploy_azure.subprocess, "run",
        lambda *args, **kwargs: calls.append(args) or subprocess.CompletedProcess([], 1, "", message))
    monkeypatch.setattr(deploy_azure.time, "sleep",
                        lambda seconds: pytest.fail(f"unexpected {seconds}s sleep"))

    with pytest.raises(SystemExit):
        deploy_azure.run_quiet("containerapp", "job", "update", retry_internal=retry_internal)

    assert len(calls) == 1


def test_access_errors_keep_existing_retry_behavior(monkeypatch):
    attempts = iter([subprocess.CompletedProcess([], 1, "", "Forbidden"), subprocess.CompletedProcess([], 0, "", "")])
    sleeps = []
    monkeypatch.setattr(deploy_azure.subprocess, "run",
                        lambda *args, **kwargs: next(attempts))
    monkeypatch.setattr(deploy_azure.time, "sleep", sleeps.append)

    deploy_azure.run_quiet("keyvault", "secret", "set", retry_access=True)

    assert sleeps == [10]


def test_job_create_retries_an_internal_error(monkeypatch):
    attempts = iter([subprocess.CompletedProcess([], 1, "", "InternalServerError"), subprocess.CompletedProcess([], 0, "", "")])
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return next(attempts)

    monkeypatch.setattr(deploy_azure.subprocess, "run", run)
    monkeypatch.setattr(deploy_azure.time, "sleep", lambda seconds: None)

    deploy_azure_container_apps.reconcile_job(
        {"resource_group": "pdt", "environment": "pdt", "registry": "pdtregistry"},
        "pdt-report", "pdtregistry.azurecr.io/report:latest", "0 0 * * *",
        "/identity/pdt-runner", "https://vault.vault.azure.net/secrets/env", False, "report")

    assert len(calls) == 2
    assert calls[0] == calls[1]
