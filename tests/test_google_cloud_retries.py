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


def test_a_new_service_account_missing_from_an_iam_binding_retries(monkeypatch):
    attempts = iter([
        subprocess.CompletedProcess(
            [], 1, "", "HTTPError 400: Service account "
            "pdt-runner@p.iam.gserviceaccount.com does not exist."),
        subprocess.CompletedProcess([], 0, "{}", ""),
    ])
    calls = []
    sleeps = []

    def run(command, **kwargs):
        calls.append(command)
        return next(attempts)

    monkeypatch.setattr(deploy_google_cloud.subprocess, "run", run)
    monkeypatch.setattr(deploy_google_cloud.time, "sleep", sleeps.append)

    assert deploy_google_cloud.run_quiet(
        "storage", "buckets", "add-iam-policy-binding", "gs://pdt-data") == "{}"
    assert calls == [calls[0], calls[0]]
    assert sleeps == [10]


SERVICE_DISABLED = ("ERROR: (gcloud.artifacts.repositories.describe) PERMISSION_DENIED: "
                    "Artifact Registry API has not been used in project p before or it is disabled.\n"
                    "- '@type': type.googleapis.com/google.rpc.ErrorInfo\n"
                    "  metadata:\n"
                    "    serviceTitle: Artifact Registry API\n"
                    "  reason: SERVICE_DISABLED\n")


def fake_gcloud(monkeypatch, results):
    attempts = iter(results)
    calls = []
    sleeps = []

    def run(command, **kwargs):
        calls.append(command)
        return next(attempts)

    monkeypatch.setattr(deploy_google_cloud.subprocess, "run", run)
    monkeypatch.setattr(deploy_google_cloud.time, "sleep", sleeps.append)
    return calls, sleeps


def test_read_json_or_none_waits_for_a_freshly_enabled_api(monkeypatch):
    calls, sleeps = fake_gcloud(monkeypatch, [
        subprocess.CompletedProcess([], 1, "", SERVICE_DISABLED),
        subprocess.CompletedProcess([], 0, '{"name": "pdt"}', ""),
    ])

    assert deploy_google_cloud.read_json_or_none(
        "artifacts", "repositories", "describe", "pdt") == {"name": "pdt"}
    assert len(calls) == 2
    assert sleeps == [10]


def test_describe_json_waits_for_a_freshly_enabled_api(monkeypatch):
    calls, sleeps = fake_gcloud(monkeypatch, [
        subprocess.CompletedProcess([], 1, "", SERVICE_DISABLED),
        subprocess.CompletedProcess([], 0, '{"name": "pdt"}', ""),
    ])

    assert deploy_google_cloud.describe_json("run", "jobs", "describe", "pdt-a") == {"name": "pdt"}
    assert len(calls) == 2
    assert sleeps == [10]


def test_a_missing_resource_reads_as_none_without_a_retry(monkeypatch):
    calls, sleeps = fake_gcloud(monkeypatch, [
        subprocess.CompletedProcess([], 1, "", "ERROR: NOT_FOUND: Repository pdt not found"),
    ])

    assert deploy_google_cloud.read_json_or_none("artifacts", "repositories", "describe", "pdt") is None
    assert len(calls) == 1
    assert sleeps == []


def test_the_waiting_message_names_the_api(monkeypatch, capsys):
    fake_gcloud(monkeypatch, [
        subprocess.CompletedProcess([], 1, "", SERVICE_DISABLED),
        subprocess.CompletedProcess([], 0, "{}", ""),
    ])

    deploy_google_cloud.read_json_or_none("artifacts", "repositories", "describe", "pdt")
    assert "Artifact Registry API is not ready yet" in capsys.readouterr().out


def test_run_quiet_fails_after_every_wait_is_used(monkeypatch):
    calls, sleeps = fake_gcloud(monkeypatch, [
        subprocess.CompletedProcess([], 1, "", SERVICE_DISABLED) for _ in range(7)
    ])

    with pytest.raises(SystemExit):
        deploy_google_cloud.run_quiet("artifacts", "repositories", "create", "pdt")
    assert len(calls) == 7
    assert sleeps == [10, 20, 40, 60, 60, 60]


def test_importing_the_script_turns_off_grpc_fork_support(monkeypatch):
    monkeypatch.delenv("GRPC_ENABLE_FORK_SUPPORT", raising=False)
    importlib.reload(deploy_google_cloud)
    assert deploy_google_cloud.os.environ["GRPC_ENABLE_FORK_SUPPORT"] == "0"
