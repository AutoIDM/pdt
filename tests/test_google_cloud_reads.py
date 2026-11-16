import base64
import io
import json
import subprocess
import threading
import time
import urllib.error

import pytest

from conftest import add_app
from pdt import config, deploy_google_cloud as gc, deploy_google_cloud_web as gweb
from pdt.deploy_common import FatalAPIError, RetriableAPIError
from pdt.deploy_google_cloud import rest_json

URL = "https://secretmanager.googleapis.com/v1/projects/p/secrets/s"


def http_error(status: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(URL, status, "", {}, io.BytesIO(b"{}"))


def fake_api(monkeypatch, answer):
    requests = []

    def fetch_json(request, timeout=60):
        requests.append(request)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(gc, "fetch_json", fetch_json)
    monkeypatch.setattr(gc, "ACCESS_TOKEN", ("ya29.token", time.monotonic()))
    return requests


def test_a_read_sends_the_token_pdt_holds_and_returns_the_answer(monkeypatch):
    requests = fake_api(monkeypatch, {"name": "s"})
    assert rest_json(URL, lambda: pytest.fail("fell back")) == {"name": "s"}
    [request] = requests
    assert request.full_url == URL
    assert request.get_method() == "GET"
    assert request.get_header("Authorization") == "Bearer ya29.token"


def test_a_resource_the_api_does_not_find_is_none(monkeypatch):
    fake_api(monkeypatch, FatalAPIError("404", http_error(404)))
    assert rest_json(URL, lambda: pytest.fail("fell back")) is None


@pytest.mark.parametrize("error", [FatalAPIError("403", http_error(403)),
                                   RetriableAPIError("503", http_error(503)),
                                   urllib.error.URLError("no route")])
def test_any_other_failure_reads_through_gcloud(monkeypatch, error):
    fake_api(monkeypatch, error)
    assert rest_json(URL, lambda: {"from": "gcloud"}) == {"from": "gcloud"}


def test_a_body_makes_a_post(monkeypatch):
    requests = fake_api(monkeypatch, {})
    rest_json(URL, lambda: None, data=b"{}")
    assert requests[0].get_method() == "POST"


def test_the_sign_in_check_keeps_its_token_for_the_next_call(monkeypatch):
    monkeypatch.setattr(gc.subprocess, "run", lambda *a, **k:
                        subprocess.CompletedProcess(a, 0, "ya29.signed-in\n", ""))
    assert gc.have_credentials()
    monkeypatch.setattr(gc, "run_quiet", lambda *args, **kwargs: pytest.fail(args))
    assert gc.access_token() == "ya29.signed-in"


def test_an_old_token_is_read_again(monkeypatch):
    calls = []
    monkeypatch.setattr(gc, "run_quiet", lambda *args, **kwargs: calls.append(args) or "ya29.new\n")
    monkeypatch.setattr(gc, "ACCESS_TOKEN", (
        "ya29.old", time.monotonic() - gc.TOKEN_REUSE_SECONDS))
    assert gc.access_token() == "ya29.new"
    assert calls == [("auth", "print-access-token")]


def paged(monkeypatch, pages):
    urls = []

    def read(url, fallback, data=None):
        urls.append(url)
        return pages[len(urls) - 1]

    monkeypatch.setattr(gc, "rest_json", read)
    return urls


def test_service_accounts_are_read_over_every_page(monkeypatch):
    urls = paged(monkeypatch, [{"accounts": [{"email": "a@p"}], "nextPageToken": "t2"},
                               {"accounts": [{"email": "b@p"}]}])
    assert [item["email"] for item in gc.service_accounts("p")] == ["a@p", "b@p"]
    assert urls == ["https://iam.googleapis.com/v1/projects/p/serviceAccounts?pageSize=100&pageToken=",
                    "https://iam.googleapis.com/v1/projects/p/serviceAccounts?pageSize=100&pageToken=t2"]


def test_the_secret_value_is_the_decoded_payload(monkeypatch):
    payload = json.dumps({"TOKEN": "é"})
    urls = paged(monkeypatch, [{"payload": {"data": base64.b64encode(payload.encode()).decode()}},
                               None])
    assert gc.secret_value("p", "pdt-report-env") == payload
    assert urls[0].endswith("/projects/p/secrets/pdt-report-env/versions/latest:access")
    assert gc.secret_value("p", "pdt-report-env") is None


def test_a_held_read_is_a_read_grant_on_another_apps_folder(monkeypatch):
    sa = "pdt-web-dashboard@p.iam.gserviceaccount.com"
    policies = {"warehouse": [{"role": gweb.READ_ROLE, "members": [f"serviceAccount:{sa}"]}],
                "dashboard": [{"role": gweb.READ_ROLE, "members": [f"serviceAccount:{sa}"]}],
                "other": [{"role": gc.STORE_ROLE, "members": [f"serviceAccount:{sa}"]}],
                "sales": [{"role": gweb.READ_ROLE, "members": ["serviceAccount:x@p"]}]}
    urls = []

    def read(url, fallback, data=None):
        urls.append(url)
        if url.endswith("managedFolders?pageToken="):
            return {"items": [{"name": "warehouse/"}, {"name": "dashboard/"}],
                    "nextPageToken": "t2"}
        if url.endswith("managedFolders?pageToken=t2"):
            return {"items": [{"name": "other/"}, {"name": "sales/"}]}
        folder = url.split("/managedFolders/")[1].split("%2F")[0]
        return {"bindings": policies[folder]}

    monkeypatch.setattr(gc, "rest_json", read)
    assert gweb.held_reads("pdt-data-x", sa, "dashboard") == ["warehouse"]
    assert ("https://storage.googleapis.com/storage/v1/b/pdt-data-x/managedFolders/warehouse%2F/iam"
            "?optionsRequestedPolicyVersion=3") in urls
    assert not any("/managedFolders/dashboard" in url for url in urls)


def test_a_bucket_that_does_not_exist_holds_no_reads(monkeypatch):
    monkeypatch.setattr(gc, "rest_json", lambda url, fallback, data=None: None)
    assert gweb.held_reads("pdt-data-x", "sa@p", "dashboard") == []


def test_the_project_number_comes_from_gcloud_when_the_api_fails(monkeypatch):
    monkeypatch.setattr(gc, "rest_json", lambda url, fallback, data=None: fallback())
    monkeypatch.setattr(gc, "run_quiet", lambda *args, **kwargs: "123456789\n")
    assert gweb.project_number("p") == "123456789"
    monkeypatch.setattr(gc, "rest_json", lambda url, fallback, data=None:
                        {"projectNumber": "987654321"})
    assert gweb.project_number("p") == "987654321"


def test_the_iap_members_are_read_from_the_service_policy(monkeypatch):
    sent = []

    def read(url, fallback, data=None):
        sent.append((url, json.loads(data)))
        return {"bindings": [{"role": gweb.IAP_ROLE, "members": ["domain:example.com"]},
                             {"role": "roles/other", "members": ["user:x@example.com"]}]}

    monkeypatch.setattr(gc, "rest_json", read)
    assert gweb.iap_policy_members("p", "123", "us-central1", "pdt-web") == ["domain:example.com"]
    assert sent == [("https://iap.googleapis.com/v1/projects/123/iap_web/cloud_run-us-central1/"
                     "services/pdt-web:getIamPolicy", {"options": {"requestedPolicyVersion": 3}})]


def test_the_plan_reads_the_resources_at_the_same_time(project, monkeypatch):
    lock = threading.Lock()
    running = [0]
    most = [0]

    def read(url, fallback, data=None):
        with lock:
            running[0] += 1
            most[0] = max(most[0], running[0])
        time.sleep(0.05)
        with lock:
            running[0] -= 1
        if "/serviceAccounts" in url:
            return {"accounts": []}
        return None

    (project / "pdt.yml").write_text(
        "platform:\n  provider: google-cloud\n  project: my-project\n  region: us-central1\n")
    add_app(project, "report", "schedule: daily\nstorage: false\n")
    monkeypatch.setattr(gc, "rest_json", read)
    monkeypatch.setattr(gc, "build_in_cloud", lambda app, builder, image_platform: False)
    monkeypatch.setattr(gc, "preflight", lambda app, project, assume_yes: "my-project")
    monkeypatch.setattr(gc, "ensure_apis", lambda project, assume_yes, apis: False)
    monkeypatch.setattr(gc, "gather_secrets", lambda app: {"TOKEN": "secret"})
    monkeypatch.setattr(gc, "cost_estimate", lambda *args, **kwargs: None)
    monkeypatch.setattr(gc, "run_quiet", lambda *args, **kwargs: pytest.fail(args))
    assert gc.deploy(config.merged_app("report"), True, plan_only=True) == 0
    assert most[0] >= 5


def test_the_first_check_in_order_names_the_problem(project, monkeypatch, capsys):
    def read(url, fallback, data=None):
        if "/serviceAccounts" in url:
            return {"accounts": []}
        if "/repositories/" in url or url.endswith("/jobs/pdt-report"):
            return {"labels": {"owner": "someone"}, "metadata": {"labels": {}}}
        return None

    (project / "pdt.yml").write_text(
        "platform:\n  provider: google-cloud\n  project: my-project\n  region: us-central1\n")
    add_app(project, "report", "schedule: daily\nstorage: false\n")
    monkeypatch.setattr(gc, "rest_json", read)
    monkeypatch.setattr(gc, "build_in_cloud", lambda app, builder, image_platform: False)
    monkeypatch.setattr(gc, "preflight", lambda app, project, assume_yes: "my-project")
    monkeypatch.setattr(gc, "ensure_apis", lambda project, assume_yes, apis: False)
    monkeypatch.setattr(gc, "gather_secrets", lambda app: {})
    with pytest.raises(SystemExit):
        gc.deploy(config.merged_app("report"), True, plan_only=True)
    out = capsys.readouterr().out
    assert "Artifact Registry repository pdt exists but is not managed by PDT" in out
    assert "Cloud Run job" not in out
