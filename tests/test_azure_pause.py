import json

import pytest

from pdt import deploy_azure, deploy_azure_container_apps

SETTINGS = {
    "subscription": "sub-1", "resource_group": "pdt", "suffix": "aaba0d5",
    "environment": deploy_azure.Environment("pdt-shared", "pdt-eastus", True),
}
JOB_URL = ("https://management.azure.com/subscriptions/sub-1/resourceGroups/pdt/providers"
           "/Microsoft.App/jobs/pdt-my-app-aaba0d5")


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
    return calls


def test_pause_suspends_a_ready_job_through_the_resource_manager_api(monkeypatch, capsys):
    calls = fake_az(monkeypatch, "Ready")
    assert deploy_azure_container_apps.pause({"name": "my-app"}, SETTINGS, True) == 0
    version = deploy_azure_container_apps.JOBS_API_VERSION
    assert calls == [
        ("rest", "--method", "get", "--url", f"{JOB_URL}?api-version={version}"),
        ("rest", "--method", "post", "--url", f"{JOB_URL}/suspend?api-version={version}"),
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
    deploy_azure_container_apps.set_job_paused(SETTINGS, "pdt-my-app-aaba0d5", paused)
    assert [call[:3] for call in calls] == [("rest", "--method", "get")]


def test_start_runs_the_job_and_names_the_execution(monkeypatch, capsys):
    calls = fake_az(monkeypatch, "Ready")
    assert deploy_azure_container_apps.start({"name": "my-app"}, SETTINGS) == 0
    assert calls == [("containerapp", "job", "start", "--name", "pdt-my-app-aaba0d5",
                      "--resource-group", "pdt", "--output", "json")]
    out = capsys.readouterr().out
    assert "pdt-my-app-abc12" in out
    assert "pdt runs my-app" in out
