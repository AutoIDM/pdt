import datetime
import subprocess

import pytest

from pdt import deploy_azure
from pdt import deploy_azure_container_apps
from pdt import runs_cli


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
    out, err = capsys.readouterr()
    assert "correlation ID" not in out
    assert "correlation ID: first" in err


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

    out, err = capsys.readouterr()
    assert all(f"correlation ID: {name}" in err for name in ("one", "two", "three"))
    assert "correlation ID: four" in out
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
        None, "report", "f" * 32)

    assert len(calls) == 2
    assert calls[0] == calls[1]


class FakeCli:
    """Sets up its log handler once, on sys.stderr as it is then, and turns Ctrl+C during a
    `rest` call into exit code 1, as knack does."""

    def invoke(self, args, out_file):
        import logging
        import types
        self.result = types.SimpleNamespace(error=None)
        if args[0] == "rest":
            try:
                raise KeyboardInterrupt
            except KeyboardInterrupt as exc:
                self.result.error = exc
                return 1
        logger = logging.getLogger("cli")
        logger.propagate = False
        if not logger.handlers:
            logger.addHandler(logging.StreamHandler())
        if args[0] == "fail":
            logger.error("ERROR: %s", args[1])
            raise SystemExit(2)
        out_file.write(" ".join(args))
        return 0


@pytest.fixture
def fake_cli(monkeypatch):
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


def test_az_runs_in_process_and_keeps_each_call_output_apart(fake_cli):
    first = deploy_azure.az("fail", "one")
    second = deploy_azure.az("fail", "two")
    third = deploy_azure.az("group", "list")

    assert (first.returncode, first.stderr) == (2, "ERROR: one\n")
    assert (second.returncode, second.stderr) == (2, "ERROR: two\n")
    assert (third.returncode, third.stdout, third.stderr) == (0, "group list", "")


SETTINGS = {"subscription": "123456789012", "resource_group": "pdt", "workspace": "pdt-logs",
            "environment": deploy_azure.Environment("pdt-shared", "pdt-eastus", True)}


def test_ctrl_c_during_an_az_call_stops_pdt(fake_cli):
    with pytest.raises(KeyboardInterrupt):
        deploy_azure.az("rest", "--method", "get", "--url", "https://management.azure.com/")


def test_ctrl_c_during_a_log_analytics_query_is_not_an_azure_error(fake_cli, monkeypatch, capsys):
    monkeypatch.setattr(deploy_azure_container_apps, "az_tsv", lambda *args: "workspace-id")
    with pytest.raises(KeyboardInterrupt):
        deploy_azure_container_apps.log_query(SETTINGS, "ContainerAppConsoleLogs_CL")
    assert "did not answer" not in capsys.readouterr().out


def test_ctrl_c_while_following_the_live_log_stops_the_follow(fake_cli, monkeypatch, capsys):
    started = datetime.datetime(2026, 10, 27, 9, 0, tzinfo=datetime.UTC)
    running = runs_cli.Run("pdt-report-x1", started, None, "running")
    ended = runs_cli.Run("pdt-report-x1", started, started, "succeeded", 0)
    monkeypatch.setattr(deploy_azure_container_apps, "az_tsv", lambda *args: "workspace-id")
    monkeypatch.setattr(runs_cli.time, "sleep", lambda seconds: None)

    code = runs_cli.follow(
        lambda: [ended],
        lambda run, since: deploy_azure_container_apps.read_lines(SETTINGS, "pdt-report", run.id,
                                                                  since),
        None, running, False, runs_cli.LOG_DELAY, [], store="Azure Log Analytics",
        stream=lambda run, follow: deploy_azure_container_apps.stream_lines(
            SETTINGS, "pdt-report", run, follow))

    assert code == 130
    out = capsys.readouterr().out
    assert "cannot read the run's live log" not in out
    assert "did not answer" not in out
