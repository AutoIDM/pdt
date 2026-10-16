import io
import json
import subprocess
import time
import urllib.error

import pytest

from pdt import deploy_google_cloud
from pdt.deploy_common import FatalAPIError


def fake_gcloud(monkeypatch):
    calls = []

    def run_quiet(*args, **kwargs):
        calls.append(args)
        if args[:3] == ("run", "jobs", "execute"):
            return json.dumps({"metadata": {"name": "pdt-my-app-k7x2m"}})
        return ""

    monkeypatch.setattr(deploy_google_cloud, "run_quiet", run_quiet)
    return calls


def fake_scheduler(monkeypatch, *error_statuses):
    requests = []
    replies = iter(error_statuses)

    def fetch_json(request, timeout=60):
        requests.append(request)
        status = next(replies, None)
        if status is None:
            return {"name": "projects/p/locations/us-central1/jobs/pdt-my-app"}
        body = io.BytesIO(json.dumps({"error": {"message": f"the {status} reason"}}).encode())
        raise FatalAPIError(f"{status}", urllib.error.HTTPError(request.full_url, status, "", {},
                                                                 body))

    monkeypatch.setattr(deploy_google_cloud, "fetch_json", fetch_json)
    monkeypatch.setattr(deploy_google_cloud, "ACCESS_TOKEN", ("ya29.token", time.monotonic()))
    monkeypatch.setattr(deploy_google_cloud.time, "sleep", lambda seconds: None)
    return requests


@pytest.mark.parametrize("paused,verb", [(True, "pause"), (False, "resume")])
def test_pause_and_unpause_are_one_call_with_the_token_pdt_has(monkeypatch, paused, verb):
    gcloud_calls = fake_gcloud(monkeypatch)
    requests = fake_scheduler(monkeypatch)
    deploy_google_cloud.set_scheduler_paused("p", "us-central1", "pdt-my-app", paused)
    [request] = requests
    assert request.get_method() == "POST"
    assert request.full_url == ("https://cloudscheduler.googleapis.com/v1/projects/p/locations/"
                                f"us-central1/jobs/pdt-my-app:{verb}")
    assert request.get_header("Authorization") == "Bearer ya29.token"
    assert gcloud_calls == []


def test_the_sign_in_check_keeps_its_token_for_the_next_call(monkeypatch):
    monkeypatch.setattr(deploy_google_cloud.subprocess, "run", lambda *a, **k:
                        subprocess.CompletedProcess(a, 0, "ya29.signed-in\n", ""))
    assert deploy_google_cloud.have_credentials()
    gcloud_calls = fake_gcloud(monkeypatch)
    assert deploy_google_cloud.access_token() == "ya29.signed-in"
    assert gcloud_calls == []


def test_an_old_token_is_read_again(monkeypatch):
    gcloud_calls = fake_gcloud(monkeypatch)
    monkeypatch.setattr(deploy_google_cloud, "ACCESS_TOKEN", (
        "ya29.old", time.monotonic() - deploy_google_cloud.TOKEN_REUSE_SECONDS))
    deploy_google_cloud.access_token()
    assert gcloud_calls == [("auth", "print-access-token")]


def test_a_missing_scheduler_job_says_deploy_first(monkeypatch, capsys):
    fake_scheduler(monkeypatch, 404)
    with pytest.raises(SystemExit):
        deploy_google_cloud.set_scheduler_paused("p", "us-central1", "pdt-my-app", True)
    assert "run pdt deploy my-app first" in capsys.readouterr().out


def test_an_aborted_call_retries(monkeypatch):
    requests = fake_scheduler(monkeypatch, 409, 409)
    deploy_google_cloud.set_scheduler_paused("p", "us-central1", "pdt-my-app", True)
    assert len(requests) == 3


def test_a_refused_call_names_the_reason(monkeypatch, capsys):
    requests = fake_scheduler(monkeypatch, 403)
    with pytest.raises(SystemExit):
        deploy_google_cloud.set_scheduler_paused("p", "us-central1", "pdt-my-app", False)
    out = capsys.readouterr().out
    assert "the 403 reason" in out
    assert "did not resume pdt-my-app" in out
    assert len(requests) == 1


def test_start_executes_the_job_and_names_the_execution(monkeypatch, capsys):
    calls = fake_gcloud(monkeypatch)
    monkeypatch.setattr(deploy_google_cloud, "project_region", lambda app: ("p", "us-central1"))
    monkeypatch.setattr(deploy_google_cloud, "preflight", lambda app, project, assume_yes: project)
    assert deploy_google_cloud.start({"name": "my-app"}, False) == 0
    assert calls == [("run", "jobs", "execute", "pdt-my-app", "--region", "us-central1",
                      "--project", "p", "--async", "--format=json",
                      "--update-env-vars=PDT_TRIGGER=manual")]
    assert "pdt-my-app-k7x2m" in capsys.readouterr().out
