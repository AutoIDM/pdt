import importlib
import subprocess

import pytest

from pdt import deploy_google_cloud


def test_a_scheduler_aborted_error_retries_with_the_same_arguments(monkeypatch):
    attempts = iter([
        subprocess.CompletedProcess([], 1, "", "ERROR: (gcloud.scheduler.jobs.delete) "
                                    "ABORTED: sync mutate calls cannot be queued"),
        subprocess.CompletedProcess([], 0, "{}", ""),
    ])
    calls = []
    sleeps = []

    def run(command, **kwargs):
        calls.append(command)
        return next(attempts)

    monkeypatch.setattr(deploy_google_cloud.subprocess, "run", run)
    monkeypatch.setattr(deploy_google_cloud.time, "sleep", sleeps.append)

    assert deploy_google_cloud.run_quiet("scheduler", "jobs", "delete", "pdt-a") == "{}"
    assert calls == [calls[0], calls[0]]
    assert sleeps == [10]


def test_a_permission_error_fails_without_a_retry(monkeypatch):
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess([], 1, "", "ERROR: PERMISSION_DENIED")

    monkeypatch.setattr(deploy_google_cloud.subprocess, "run", run)
    monkeypatch.setattr(deploy_google_cloud.time, "sleep", lambda _: None)

    with pytest.raises(SystemExit):
        deploy_google_cloud.run_quiet("scheduler", "jobs", "delete", "pdt-a")
    assert len(calls) == 1


def test_importing_the_script_turns_off_grpc_fork_support(monkeypatch):
    monkeypatch.delenv("GRPC_ENABLE_FORK_SUPPORT", raising=False)
    importlib.reload(deploy_google_cloud)
    assert deploy_google_cloud.os.environ["GRPC_ENABLE_FORK_SUPPORT"] == "0"
