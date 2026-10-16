import json
import subprocess

import pytest

from pdt import deploy_azure, deploy_azure_container_apps

SETTINGS = {
    "subscription": "sub-1", "resource_group": "pdt", "suffix": "abc1234def",
    "environment": deploy_azure.Environment("pdt-shared", "pdt-eastus", True),
}
JOB = deploy_azure_container_apps.job_name(SETTINGS, "my-app")
JOB_URL = ("https://management.azure.com/subscriptions/sub-1/resourceGroups/pdt/providers"
           f"/Microsoft.App/jobs/{JOB}")


def fake_az(monkeypatch, running_state: str):
    calls = []

    def run_quiet(*args, **kwargs):
        calls.append(args)
        if args[:3] == ("rest", "--method", "get"):
            return json.dumps({"properties": {"runningState": running_state}})
        if args[:3] == ("containerapp", "job", "start"):
            return json.dumps({"name": "pdt-my-app-abc12"})
        return ""

    monkeypatch.setattr(deploy_azure_container_apps, "run_quiet", run_quiet)
    # find_job looks the job up under its current name first.
    monkeypatch.setattr(deploy_azure_container_apps, "az_json", lambda *args: {"name": args[4]})
    return calls


def test_pause_suspends_a_ready_job_through_the_resource_manager_api(monkeypatch, capsys):
    calls = fake_az(monkeypatch, "Ready")
    assert deploy_azure_container_apps.pause({"name": "my-app"}, SETTINGS, True) == 0
    version = deploy_azure_container_apps.JOBS_API_VERSION
    assert calls == [
        ("rest", "--method", "get", "--url", f"{JOB_URL}?api-version={version}"),
        ("rest", "--method", "post", "--url", f"{JOB_URL}/suspend?api-version={version}"),
        ("rest", "--method", "get", "--url", f"{JOB_URL}?api-version={version}"),
    ]
    assert "Paused my-app" in capsys.readouterr().out


def test_unpause_resumes_a_suspended_job(monkeypatch):
    calls = fake_az(monkeypatch, "Suspended")
    assert deploy_azure_container_apps.pause({"name": "my-app"}, SETTINGS, False) == 0
    assert calls[1][:3] == ("rest", "--method", "post")
    assert calls[1][4].startswith(f"{JOB_URL}/resume?")


@pytest.mark.parametrize("state,paused", [("Suspended", True), ("Ready", False)])
def test_a_job_already_in_the_wanted_state_is_left_alone(monkeypatch, state, paused):
    calls = fake_az(monkeypatch, state)
    deploy_azure_container_apps.set_job_paused(SETTINGS, JOB, paused)
    assert [call[:3] for call in calls] == [("rest", "--method", "get")]


def test_start_runs_the_job_with_its_own_container_and_a_manual_trigger(monkeypatch, capsys):
    calls = fake_az(monkeypatch, "Ready")
    container = {"name": JOB, "image": "pdtabc.azurecr.io/pdt-my-app:1",
                 "resources": {"cpu": 0.5, "memory": "1Gi"},
                 "env": [{"name": "PDT_ENV_JSON", "secretRef": "pdt-env"},
                         {"name": "PDT_STORAGE_URL", "value": "https://store/x"}]}
    monkeypatch.setattr(deploy_azure_container_apps, "az_json", lambda *args: {
        "name": args[4], "properties": {"template": {"containers": [container]}}})
    assert deploy_azure_container_apps.start({"name": "my-app"}, SETTINGS) == 0
    assert calls == [("containerapp", "job", "start", "--name", JOB, "--resource-group", "pdt",
                      "--container-name", JOB, "--image", "pdtabc.azurecr.io/pdt-my-app:1",
                      "--cpu", "0.5", "--memory", "1Gi",
                      "--env-vars", "PDT_ENV_JSON=secretref:pdt-env",
                      "PDT_STORAGE_URL=https://store/x", "PDT_TRIGGER=manual",
                      "--output", "json")]
    assert "pdt-my-app-abc12" in capsys.readouterr().out


def fake_operation(monkeypatch, running_state: str, provisioning_states: list[str]):
    """A job in `running_state`; each GET after the suspend or resume shows the next state."""
    calls, sleeps = [], []
    states = iter(provisioning_states)

    def run_quiet(*args, **kwargs):
        calls.append((args, kwargs))
        if args[:3] == ("rest", "--method", "get"):
            posted = any(call[0][:3] == ("rest", "--method", "post") for call in calls)
            state = next(states) if posted else "Succeeded"
            return json.dumps({"properties": {"runningState": running_state,
                                              "provisioningState": state}})
        return ""

    monkeypatch.setattr(deploy_azure_container_apps, "run_quiet", run_quiet)
    monkeypatch.setattr(deploy_azure_container_apps.time, "sleep", sleeps.append)
    return calls, sleeps


def test_pause_returns_only_after_azure_finishes_the_suspend(monkeypatch):
    calls, sleeps = fake_operation(monkeypatch, "Ready", ["InProgress", "InProgress", "Succeeded"])
    deploy_azure_container_apps.set_job_paused(SETTINGS, JOB, True)
    assert [call[0][2] for call in calls] == ["get", "post", "get", "get", "get"]
    assert sleeps == [deploy_azure_container_apps.JOB_UPDATE_POLL_SECONDS] * 2


def test_a_suspend_that_azure_could_not_finish_fails(monkeypatch):
    fake_operation(monkeypatch, "Ready", ["InProgress", "Failed"])
    with pytest.raises(SystemExit):
        deploy_azure_container_apps.set_job_paused(SETTINGS, JOB, True)


def test_the_suspend_retries_while_another_change_to_the_job_runs(monkeypatch):
    calls, _sleeps = fake_operation(monkeypatch, "Ready", ["Succeeded"])
    deploy_azure_container_apps.set_job_paused(SETTINGS, JOB, True)
    post = next(call for call in calls if call[0][2] == "post")
    assert post[1] == {"retry_busy": True}


def test_destroy_deletes_the_job_without_waiting_out_retry_after_and_polls_every_2s(monkeypatch):
    calls, sleeps = [], []
    gets = iter([subprocess.CompletedProcess([], 0, "{}", ""), subprocess.CompletedProcess([], 0, "{}", ""),
                 subprocess.CompletedProcess([], 1, "", 'ERROR: Not Found({"error":{"code":"ContainerAppsJobNotFound"}})')])
    monkeypatch.setattr(deploy_azure_container_apps, "run_quiet",
                        lambda *args, **kwargs: calls.append(args) or "")
    monkeypatch.setattr(deploy_azure_container_apps, "az", lambda *args: calls.append(args) or next(gets))
    monkeypatch.setattr(deploy_azure_container_apps.time, "sleep", sleeps.append)
    deploy_azure_container_apps.delete_job(SETTINGS, JOB)
    assert calls[0] == ("containerapp", "job", "delete", "--name", JOB, "--resource-group", "pdt",
                        "--yes", "--no-wait")
    assert [call[:3] for call in calls[1:]] == [("rest", "--method", "get")] * 3
    assert sleeps == [2, 2]


def test_a_job_azure_never_finishes_deleting_fails(monkeypatch):
    monkeypatch.setattr(deploy_azure_container_apps, "run_quiet", lambda *args, **kwargs: "")
    monkeypatch.setattr(deploy_azure_container_apps, "az",
                        lambda *args: subprocess.CompletedProcess([], 0, "{}", ""))
    monkeypatch.setattr(deploy_azure_container_apps.time, "sleep", lambda seconds: None)
    with pytest.raises(SystemExit):
        deploy_azure_container_apps.delete_job(SETTINGS, JOB)
