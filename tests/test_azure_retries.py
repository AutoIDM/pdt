import subprocess

import pytest

from pdt import deploy_azure, deploy_azure_container_apps


def result(returncode: int, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], returncode, stdout="", stderr=stderr)


def job_settings() -> dict:
    return {
        "resource_group": "pdt",
        "subscription": "sub-1",
        "registry": "pdtregistry",
        "environment": deploy_azure.Environment("pdt-shared", "pdt-eastus", True),
    }


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


def test_an_azure_internal_error_is_retried_with_backoff(monkeypatch):
    attempts = iter([
        result(1, "ERROR: (InternalServerError) correlation ID: first"),
        result(1, "ERROR: Internal server error occurred. correlation ID: second"),
        result(0),
    ])
    sleeps = []
    monkeypatch.setattr(deploy_azure.subprocess, "run", lambda *args, **kwargs: next(attempts))
    monkeypatch.setattr(deploy_azure.time, "sleep", sleeps.append)

    deploy_azure.run_quiet("containerapp", "job", "update", retry_internal=True)

    assert sleeps == [10, 20]


def test_a_resource_created_despite_an_internal_error_is_reconciled(monkeypatch, capsys):
    monkeypatch.setattr(
        deploy_azure.subprocess, "run",
        lambda *args, **kwargs: result(
            1, "ERROR: (InternalServerError) correlation ID: recovered"))
    monkeypatch.setattr(
        deploy_azure.time, "sleep",
        lambda seconds: pytest.fail(f"unexpected {seconds}s sleep"))

    deploy_azure.run_quiet(
        "containerapp", "job", "create", retry_internal=True,
        recovered=lambda: True)

    assert "finishing its configuration" in capsys.readouterr().out


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


def test_the_last_azure_internal_error_and_correlation_id_are_shown(monkeypatch, capsys):
    attempts = []

    def fail(*args, **kwargs):
        attempts.append(args)
        return result(1, "ERROR: (InternalServerError) correlation ID: final-id")

    monkeypatch.setattr(deploy_azure.subprocess, "run", fail)
    monkeypatch.setattr(deploy_azure.time, "sleep", lambda seconds: None)

    with pytest.raises(SystemExit):
        deploy_azure.run_quiet(
            "containerapp", "job", "create", "--name", "pdt-example",
            retry_internal=True)

    output = capsys.readouterr().out
    assert len(attempts) == 4
    assert "correlation ID: final-id" in output
    assert "failed after 4 attempts" in output


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
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        # Only the first create fails; the rest of the reconcile succeeds, so
        # the two create commands are the only ones to compare.
        if len([call for call in calls if "create" in call]) == 1 and "create" in command:
            return subprocess.CompletedProcess([], 1, "", "InternalServerError")
        return subprocess.CompletedProcess([], 0, "", "")

    monkeypatch.setattr(deploy_azure.subprocess, "run", run)
    monkeypatch.setattr(deploy_azure.time, "sleep", lambda seconds: None)

    deploy_azure_container_apps.reconcile_job(
        job_settings(),
        "pdt-report", "pdtregistry.azurecr.io/report:latest", "0 0 * * *",
        "/identity/pdt-runner", "https://vault.vault.azure.net/secrets/env", None,
        False, "report")

    creates = [call for call in calls if "create" in call]
    assert len(creates) == 2
    assert creates[0] == creates[1]


def test_a_new_container_app_job_is_updated_after_create(monkeypatch):
    calls = []

    def run(*args, **kwargs):
        calls.append((args, kwargs))
        return ""

    monkeypatch.setattr(deploy_azure_container_apps, "run_quiet", run)
    monkeypatch.setattr(
        deploy_azure_container_apps, "az_json",
        lambda *args: {"name": "pdt-example"})

    deploy_azure_container_apps.reconcile_job(
        job_settings(), "pdt-example", "pdtregistry.azurecr.io/example:latest",
        "0 0 * * *", "/identity/pdt-runner", "https://vault/secret",
        None, False, "example")

    create = calls[0]
    assert create[0][:3] == ("containerapp", "job", "create")
    assert create[1]["retry_internal"] is True
    assert create[1]["recovered"]() is True
    assert any(call[0][:3] == ("containerapp", "job", "update") for call in calls)
