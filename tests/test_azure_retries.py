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

    def run(*command):
        calls.append((command, {}))
        return next(attempts)

    monkeypatch.setattr(deploy_azure, "az", run)
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
    monkeypatch.setattr(deploy_azure, "az", lambda *args: next(attempts))
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
        deploy_azure, "az",
        lambda *args: calls.append(args) or subprocess.CompletedProcess([], 1, "", "Forbidden"))
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
        deploy_azure, "az",
        lambda *args: calls.append(args) or subprocess.CompletedProcess([], 1, "", message))
    monkeypatch.setattr(deploy_azure.time, "sleep",
                        lambda seconds: pytest.fail(f"unexpected {seconds}s sleep"))

    with pytest.raises(SystemExit):
        deploy_azure.run_quiet("containerapp", "job", "update", retry_internal=retry_internal)

    assert len(calls) == 1


def test_access_errors_keep_existing_retry_behavior(monkeypatch):
    attempts = iter([subprocess.CompletedProcess([], 1, "", "Forbidden"), subprocess.CompletedProcess([], 0, "", "")])
    sleeps = []
    monkeypatch.setattr(deploy_azure, "az",
                        lambda *args: next(attempts))
    monkeypatch.setattr(deploy_azure.time, "sleep", sleeps.append)

    deploy_azure.run_quiet("keyvault", "secret", "set", retry_access=True)

    assert sleeps == [10]


def test_job_create_retries_an_internal_error(monkeypatch):
    attempts = iter([subprocess.CompletedProcess([], 1, "", "InternalServerError"), subprocess.CompletedProcess([], 0, "", "")])
    calls = []

    def run(*command):
        calls.append(command)
        return next(attempts)

    monkeypatch.setattr(deploy_azure, "az", run)
    monkeypatch.setattr(deploy_azure.time, "sleep", lambda seconds: None)

    deploy_azure_container_apps.reconcile_job(
        {"resource_group": "pdt", "subscription": "sub-1", "registry": "pdtregistry",
         "environment": deploy_azure.Environment("pdt-shared", "pdt-eastus", True)},
        "pdt-report", "pdtregistry.azurecr.io/report:latest", "0 0 * * *",
        "/identity/pdt-runner", "https://vault.vault.azure.net/secrets/env", None,
        False, "report")

    assert len(calls) == 2
    assert calls[0] == calls[1]


class FakeCli:
    """Sets up its log handler once, on sys.stderr as it is then, as knack does."""

    def invoke(self, args, out_file):
        import logging
        logger = logging.getLogger("cli")
        logger.propagate = False
        if not logger.handlers:
            logger.addHandler(logging.StreamHandler())
        if args[0] == "fail":
            logger.error("ERROR: %s", args[1])
            raise SystemExit(2)
        out_file.write(" ".join(args))
        return 0


def test_az_runs_in_process_and_keeps_each_call_output_apart(monkeypatch):
    import sys
    import types
    core = types.ModuleType("azure.cli.core")
    core.get_default_cli = FakeCli
    knack_log = types.ModuleType("knack.log")
    knack_log.cli_logger_names = ["cli"]
    for name, module in {"azure": types.ModuleType("azure"), "azure.cli": types.ModuleType("azure.cli"),
                         "azure.cli.core": core, "knack": types.ModuleType("knack"),
                         "knack.log": knack_log}.items():
        monkeypatch.setitem(sys.modules, name, module)

    first = deploy_azure.az("fail", "one")
    second = deploy_azure.az("fail", "two")
    third = deploy_azure.az("group", "list")

    assert (first.returncode, first.stderr) == (2, "ERROR: one\n")
    assert (second.returncode, second.stderr) == (2, "ERROR: two\n")
    assert (third.returncode, third.stdout, third.stderr) == (0, "group list", "")
