import json

import pytest

from pdt import deploy_google_cloud


def fake_gcloud(monkeypatch, state: str | None):
    calls = []

    def read_json_or_none(*args):
        calls.append(args)
        return None if state is None else {"name": "projects/p/jobs/pdt-my-app", "state": state}

    def run_quiet(*args, **kwargs):
        calls.append(args)
        if args[:3] == ("run", "jobs", "execute"):
            return json.dumps({"metadata": {"name": "pdt-my-app-k7x2m"}})
        return ""

    monkeypatch.setattr(deploy_google_cloud, "read_json_or_none", read_json_or_none)
    monkeypatch.setattr(deploy_google_cloud, "run_quiet", run_quiet)
    return calls


def test_pausing_an_enabled_job_calls_pause(monkeypatch):
    calls = fake_gcloud(monkeypatch, "ENABLED")
    deploy_google_cloud.set_scheduler_paused("p", "us-central1", "pdt-my-app", True)
    assert calls == [
        ("scheduler", "jobs", "describe", "pdt-my-app", "--location", "us-central1",
         "--project", "p"),
        ("scheduler", "jobs", "pause", "pdt-my-app", "--location", "us-central1",
         "--project", "p"),
    ]


def test_unpausing_a_paused_job_calls_resume(monkeypatch):
    calls = fake_gcloud(monkeypatch, "PAUSED")
    deploy_google_cloud.set_scheduler_paused("p", "us-central1", "pdt-my-app", False)
    assert calls[1][:3] == ("scheduler", "jobs", "resume")


@pytest.mark.parametrize("state,paused", [("PAUSED", True), ("ENABLED", False)])
def test_a_job_already_in_the_wanted_state_is_left_alone(monkeypatch, state, paused):
    calls = fake_gcloud(monkeypatch, state)
    deploy_google_cloud.set_scheduler_paused("p", "us-central1", "pdt-my-app", paused)
    assert [call[:3] for call in calls] == [("scheduler", "jobs", "describe")]


def test_a_missing_scheduler_job_says_deploy_first(monkeypatch, capsys):
    fake_gcloud(monkeypatch, None)
    with pytest.raises(SystemExit):
        deploy_google_cloud.set_scheduler_paused("p", "us-central1", "pdt-my-app", True)
    assert "run pdt deploy first" in capsys.readouterr().out


def test_start_executes_the_job_and_names_the_execution(monkeypatch, capsys):
    calls = fake_gcloud(monkeypatch, "ENABLED")
    monkeypatch.setattr(deploy_google_cloud, "project_region", lambda app: ("p", "us-central1"))
    monkeypatch.setattr(deploy_google_cloud, "preflight", lambda app, project, assume_yes: project)
    assert deploy_google_cloud.start({"name": "my-app"}, False) == 0
    assert calls == [("run", "jobs", "execute", "pdt-my-app", "--region", "us-central1",
                      "--project", "p", "--async", "--format=json")]
    out = capsys.readouterr().out
    assert "pdt-my-app-k7x2m" in out
    assert "pdt runs my-app" in out
