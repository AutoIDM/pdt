import base64
import datetime
import http.client
import io
import json
import subprocess
import threading
import time
import urllib.error
import urllib.request

import pytest

from conftest import add_app
from pdt import config, deploy_google_cloud as gc, deploy_google_cloud_web as gweb
from pdt.deploy_google_cloud import api_json

URL = "https://secretmanager.googleapis.com/v1/projects/p/secrets/s"


def http_error(status: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(URL, status, "", {}, io.BytesIO(b"{}"))


class CutOff(io.BytesIO):
    def read(self, *args):
        raise http.client.IncompleteRead(b"{\"na")


class FakeOpener:
    """The network of the real api_json: each request gets the next of `answers`, a dict to
    send as JSON, bytes to send as they are, or an exception to raise."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.requests = []
        self.lock = threading.Lock()

    def open(self, request, timeout=None):
        with self.lock:
            self.requests.append(request)
            answer = self.answers[min(len(self.requests), len(self.answers)) - 1]
        if isinstance(answer, BaseException):
            raise answer
        if isinstance(answer, io.BytesIO):
            return answer
        return io.BytesIO(answer if isinstance(answer, bytes) else json.dumps(answer).encode())


def fake_api(monkeypatch, *answers):
    opener = FakeOpener(*answers)
    monkeypatch.setattr(gc, "api_json", api_json)
    monkeypatch.setattr(gc, "API_OPENER", opener)
    monkeypatch.setattr(gc, "ACCESS_TOKEN", ("ya29.token", time.monotonic()))
    return opener.requests


def test_a_read_sends_the_token_pdt_holds_and_returns_the_answer(monkeypatch):
    requests = fake_api(monkeypatch, {"name": "s"})
    assert gc.rest_json(URL, lambda: pytest.fail("fell back")) == {"name": "s"}
    [request] = requests
    assert request.full_url == URL
    assert request.get_method() == "GET"
    assert request.get_header("Authorization") == "Bearer ya29.token"


def test_a_resource_the_api_does_not_find_is_none(monkeypatch):
    fake_api(monkeypatch, http_error(404))
    assert gc.rest_json(URL, lambda: pytest.fail("fell back")) is None


@pytest.mark.parametrize("answer", [http_error(403), http_error(503), http_error(302),
                                    urllib.error.URLError("no route"), CutOff(), b"<html>"])
def test_any_other_failure_reads_through_gcloud(monkeypatch, capsys, answer):
    requests = fake_api(monkeypatch, answer, {"name": "s"})
    assert gc.rest_json(URL, lambda: {"from": "gcloud"}) == {"from": "gcloud"}
    assert len(requests) == 1
    assert capsys.readouterr().err == ""


def test_a_body_makes_a_post(monkeypatch):
    requests = fake_api(monkeypatch, {})
    gc.rest_json(URL, lambda: None, data=b"{}")
    assert requests[0].get_method() == "POST"


def test_a_failed_read_in_a_worker_prints_nothing_and_runs_gcloud_once_asked(monkeypatch,
                                                                             capsys):
    requests = fake_api(monkeypatch, http_error(503), {"name": "s"})
    threads = []

    def fallback():
        threads.append(threading.current_thread())
        gc.console.say("ERROR: (gcloud.secrets.describe) the service is busy")
        return {"from": "gcloud"}

    with gc.ThreadPoolExecutor(2) as pool:
        read = gc.submit(pool, gc.rest_json, URL, fallback)
        time.sleep(0.1)
        assert capsys.readouterr() == ("", "")
        assert read() == {"from": "gcloud"}
    assert len(requests) == 1
    assert threads == [threading.main_thread()]
    assert "the service is busy" in capsys.readouterr().out


def test_a_read_run_again_in_the_main_thread_sends_no_request_twice(monkeypatch):
    sa = "pdt-web-dashboard@p.iam.gserviceaccount.com"
    grant = {"bindings": [{"role": gweb.READ_ROLE, "members": [f"serviceAccount:{sa}"]}]}
    sent = []

    def answer(request):
        if "managedFolders?" in request.full_url:
            return {"items": [{"name": "a/"}, {"name": "b/"}, {"name": "c/"}]}
        return http_error(403) if "/b%2F/" in request.full_url else grant

    class Opener:
        lock = threading.Lock()

        def open(self, request, timeout=None):
            with self.lock:
                sent.append(request.full_url)
            found = answer(request)
            if isinstance(found, Exception):
                raise found
            return io.BytesIO(json.dumps(found).encode())

    monkeypatch.setattr(gc, "api_json", api_json)
    monkeypatch.setattr(gc, "API_OPENER", Opener())
    monkeypatch.setattr(gc, "ACCESS_TOKEN", ("ya29.token", time.monotonic()))
    asked = []
    monkeypatch.setattr(gc, "describe_json", lambda *args: asked.append(
        (args[-1], threading.current_thread())) or {})
    with gc.ThreadPoolExecutor(2) as pool:
        read = gc.submit(pool, gweb.held_reads, "pdt-data-x", sa, "own")
        assert read() == ["a", "c"]
    assert sorted(sent) == sorted(set(sent)) and len(sent) == 4
    assert asked == [("gs://pdt-data-x/b/", threading.main_thread())]


@pytest.mark.parametrize("url", [
    "https://attacker.example/-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/p/jobs/j",
    "http://run.googleapis.com/v1/x",
    "https://googleapis.com.attacker.example/v1/x",
    "https://attacker.example/x-docker.pkg.dev",
])
def test_the_token_goes_to_no_host_but_a_google_api(monkeypatch, capsys, url):
    requests = fake_api(monkeypatch, {})
    with pytest.raises(SystemExit):
        gc.rest_json(url, lambda: pytest.fail("fell back"))
    assert requests == []
    assert "only to Google API hosts" in capsys.readouterr().out


@pytest.mark.parametrize("url", ["https://us-central1-run.googleapis.com/apis/x",
                                 "https://storage.googleapis.com/storage/v1/b/x",
                                 "https://europe-west1-docker.pkg.dev"])
def test_the_google_api_hosts(url):
    assert gc.google_host(url)


def test_a_redirect_is_followed_only_to_a_google_api_host():
    request = urllib.request.Request(URL, headers={"Authorization": "Bearer ya29.token"})
    redirects = gc.GoogleRedirects()
    assert redirects.redirect_request(request, None, 302, "Found", {},
                                      "https://attacker.example/steal") is None
    followed = redirects.redirect_request(request, None, 302, "Found", {},
                                          "https://storage.googleapis.com/storage/v1/b/x")
    assert followed.full_url == "https://storage.googleapis.com/storage/v1/b/x"


@pytest.mark.parametrize("region", ["us-central1", "northamerica-northeast2", "me-central2"])
def test_a_google_cloud_region_name_is_accepted(region):
    assert gc.project_region({"platform": {"project": "p", "region": region}}) == ("p", region)


def test_a_region_that_is_not_a_region_name_stops_with_the_file_to_change(capsys):
    with pytest.raises(SystemExit):
        gc.project_region({"platform": {"project": "p", "region": "attacker.example/"}})
    out = capsys.readouterr().out
    assert "attacker.example/ is not a Google Cloud region name" in out
    assert "change platform.region in the project's pdt.yml or the app's pdt.yml" in out


def test_a_region_from_the_environment_names_the_variable(monkeypatch, capsys):
    monkeypatch.setenv("PDT_GOOGLE_CLOUD_REGION", "us central1")
    with pytest.raises(SystemExit):
        gc.project_region({"platform": {"project": "p"}})
    assert "change PDT_GOOGLE_CLOUD_REGION" in capsys.readouterr().out


def test_the_image_push_logs_in_only_to_a_google_registry(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(gc, "run_build", lambda *args, **kwargs: pytest.fail("ran docker"))
    monkeypatch.setattr(gc, "run_quiet", lambda *args, **kwargs: pytest.fail("read the token"))
    with pytest.raises(SystemExit):
        gc.build_image(tmp_path, "image", "attacker.example/")
    assert "only to Google API hosts" in capsys.readouterr().out


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


def pages(monkeypatch, answers):
    """api_json that gives `answers` in turn; an exception in the list is raised."""
    urls = []

    def read(url, data=None):
        urls.append(url)
        answer = answers[len(urls) - 1]
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(gc, "api_json", read)
    return urls


def test_service_accounts_are_read_over_every_page(monkeypatch):
    urls = pages(monkeypatch, [{"accounts": [{"email": "a@p"}], "nextPageToken": "t2"},
                               {"accounts": [{"email": "b@p"}]}])
    assert [item["email"] for item in gc.service_accounts("p")] == ["a@p", "b@p"]
    assert urls == ["https://iam.googleapis.com/v1/projects/p/serviceAccounts?pageSize=100&pageToken=",
                    "https://iam.googleapis.com/v1/projects/p/serviceAccounts?pageSize=100&pageToken=t2"]


def test_a_failed_later_page_gives_only_the_gcloud_list(monkeypatch):
    pages(monkeypatch, [{"items": [{"name": "a/"}], "nextPageToken": "t2"},
                        gc.ApiReadError("page 2")])
    listed = gc.rest_list("https://storage.googleapis.com/storage/v1/b/x/managedFolders", "items",
                          lambda: [{"name": "a/"}, {"name": "b/"}])
    assert listed == [{"name": "a/"}, {"name": "b/"}]


def test_the_secret_value_is_the_decoded_payload(monkeypatch):
    payload = json.dumps({"TOKEN": "é>?~"})
    urls = pages(monkeypatch, [{"payload": {"data": base64.b64encode(payload.encode()).decode()}},
                               None])
    assert gc.secret_value("p", "pdt-report-env") == payload
    assert urls[0].endswith("/projects/p/secrets/pdt-report-env/versions/latest:access")
    assert gc.secret_value("p", "pdt-report-env") is None


def test_the_secret_value_from_gcloud_is_url_safe_base64(monkeypatch):
    payload = json.dumps({"TOKEN": "é>?~"})
    data = base64.urlsafe_b64encode(payload.encode()).decode()
    assert "-" in data or "_" in data
    answer = json.dumps({"payload": {"data": data}})
    monkeypatch.setattr(gc, "gcloud", lambda *args: subprocess.CompletedProcess(args, 0, answer, ""))
    assert gc.secret_value("p", "pdt-report-env") == payload


def test_a_held_read_is_a_read_grant_on_another_apps_folder(monkeypatch):
    sa = "pdt-web-dashboard@p.iam.gserviceaccount.com"
    policies = {"warehouse": [{"role": gweb.READ_ROLE, "members": [f"serviceAccount:{sa}"]}],
                "dashboard": [{"role": gweb.READ_ROLE, "members": [f"serviceAccount:{sa}"]}],
                "other": [{"role": gc.STORE_ROLE, "members": [f"serviceAccount:{sa}"]}],
                "sales": [{"role": gweb.READ_ROLE, "members": ["serviceAccount:x@p"]}]}
    urls = []

    def read(url, data=None):
        urls.append(url)
        if url.endswith("managedFolders?pageToken="):
            return {"items": [{"name": "warehouse/"}, {"name": "dashboard/"}],
                    "nextPageToken": "t2"}
        if url.endswith("managedFolders?pageToken=t2"):
            return {"items": [{"name": "other/"}, {"name": "sales/"}]}
        folder = url.split("/managedFolders/")[1].split("%2F")[0]
        return {"bindings": policies[folder]}

    monkeypatch.setattr(gc, "api_json", read)
    assert gweb.held_reads("pdt-data-x", sa, "dashboard") == ["warehouse"]
    assert ("https://storage.googleapis.com/storage/v1/b/pdt-data-x/managedFolders/warehouse%2F/iam"
            "?optionsRequestedPolicyVersion=3") in urls
    assert not any("/managedFolders/dashboard" in url for url in urls)


def test_a_bucket_that_does_not_exist_holds_no_reads(monkeypatch):
    monkeypatch.setattr(gc, "api_json", lambda url, data=None: None)
    assert gweb.held_reads("pdt-data-x", "sa@p", "dashboard") == []


def test_the_project_number_comes_from_gcloud_when_the_api_fails(monkeypatch):
    monkeypatch.setattr(gc, "run_quiet", lambda *args, **kwargs: "123456789\n")
    assert gweb.project_number("p") == "123456789"
    monkeypatch.setattr(gc, "api_json", lambda url, data=None: {"projectNumber": "987654321"})
    assert gweb.project_number("p") == "987654321"


def test_the_iap_members_are_read_from_the_service_policy(monkeypatch):
    sent = []

    def read(url, data=None):
        sent.append((url, json.loads(data)))
        return {"bindings": [{"role": gweb.IAP_ROLE, "members": ["domain:example.com"]},
                             {"role": "roles/other", "members": ["user:x@example.com"]}]}

    monkeypatch.setattr(gc, "api_json", read)
    assert gweb.iap_policy_members("p", "123", "us-central1", "pdt-web") == ["domain:example.com"]
    assert sent == [("https://iap.googleapis.com/v1/projects/123/iap_web/cloud_run-us-central1/"
                     "services/pdt-web:getIamPolicy", {"options": {"requestedPolicyVersion": 3}})]


def job_plan(project, monkeypatch, read, values=None):
    (project / "pdt.yml").write_text(
        "platform:\n  provider: google-cloud\n  project: my-project\n  region: us-central1\n")
    add_app(project, "report", "schedule: daily\nstorage: false\n")
    monkeypatch.setattr(gc, "api_json", read)
    monkeypatch.setattr(gc, "build_in_cloud", lambda app, builder, image_platform: False)
    monkeypatch.setattr(gc, "preflight", lambda app, project, assume_yes: "my-project")
    monkeypatch.setattr(gc, "ensure_apis", lambda project, assume_yes, apis: False)
    monkeypatch.setattr(gc, "gather_secrets", lambda app: values or {})
    monkeypatch.setattr(gc, "cost_estimate", lambda *args, **kwargs: None)
    return config.merged_app("report")


def test_the_plan_reads_the_resources_at_the_same_time(project, monkeypatch):
    lock = threading.Lock()
    running = [0]
    five = threading.Event()
    deadline = time.monotonic() + 10

    def read(url, data=None):
        with lock:
            running[0] += 1
            if running[0] == 5:
                five.set()
        # Each read waits for five reads at once, so a busy computer that starts a thread
        # late cannot end the first read before the fifth starts.
        five.wait(max(0, deadline - time.monotonic()))
        with lock:
            running[0] -= 1
        return {"accounts": []} if "/serviceAccounts" in url else None

    app = job_plan(project, monkeypatch, read, {"TOKEN": "secret"})
    monkeypatch.setattr(gc, "run_quiet", lambda *args, **kwargs: pytest.fail(args))
    assert gc.deploy(app, True, plan_only=True) == 0
    assert five.is_set()


def test_the_first_check_in_order_names_the_problem(project, monkeypatch, capsys):
    def read(url, data=None):
        if "/serviceAccounts" in url:
            return {"accounts": []}
        if "/repositories/" in url or url.endswith("/jobs/pdt-report"):
            return {"labels": {"owner": "someone"}, "metadata": {"labels": {}}}
        return None

    with pytest.raises(SystemExit):
        gc.deploy(job_plan(project, monkeypatch, read), True, plan_only=True)
    out = capsys.readouterr().out
    assert "Artifact Registry repository pdt exists but is not managed by PDT" in out
    assert "Cloud Run job" not in out


def test_a_gcloud_fallback_runs_in_the_main_thread_after_the_earlier_checks(project, monkeypatch,
                                                                          capsys):
    threads = []

    def read(url, data=None):
        if "/repositories/" in url:
            return {"labels": {"owner": "someone"}}
        raise gc.ApiReadError(url)

    def list_json(*args):
        threads.append(threading.current_thread())
        gc.console.say("ERROR: (gcloud.iam.service-accounts.list) PERMISSION_DENIED")
        return []

    app = job_plan(project, monkeypatch, read)
    monkeypatch.setattr(gc, "list_json", list_json)
    with pytest.raises(SystemExit):
        gc.deploy(app, True, plan_only=True)
    out = capsys.readouterr().out
    assert "Artifact Registry repository pdt exists but is not managed by PDT" in out
    assert "PERMISSION_DENIED" not in out
    assert threads == []

    def no_api(url, data=None):
        raise gc.ApiReadError(url)

    monkeypatch.setattr(gc, "api_json", no_api)
    monkeypatch.setattr(gc, "read_json_or_none", lambda *args: None)
    assert gc.deploy(app, True, plan_only=True) == 0
    assert threads == [threading.main_thread()]


def test_the_enabled_apis_are_read_over_every_page(monkeypatch):
    urls = pages(monkeypatch, [
        {"services": [{"config": {"name": api}} for api in gc.APIS[:3]], "nextPageToken": "t2"},
        {"services": [{"config": {"name": api}} for api in gc.APIS[3:]]}])
    monkeypatch.setattr(gc, "run_quiet", lambda *args, **kwargs: pytest.fail(args))
    assert gc.ensure_apis("p", True) is False
    assert urls == [
        "https://serviceusage.googleapis.com/v1/projects/p/services?filter=state:ENABLED"
        "&pageSize=200&pageToken=",
        "https://serviceusage.googleapis.com/v1/projects/p/services?filter=state:ENABLED"
        "&pageSize=200&pageToken=t2"]


def test_a_missing_api_is_enabled(monkeypatch):
    pages(monkeypatch, [{"services": [{"config": {"name": api}} for api in gc.APIS
                                      if api != "run.googleapis.com"]}])
    calls = []
    monkeypatch.setattr(gc, "run_quiet", lambda *args, **kwargs: calls.append(args) or "")
    monkeypatch.setattr(gc, "confirm", lambda actions, assume_yes: True)
    assert gc.ensure_apis("p", True) is False
    assert calls == [("services", "enable", "run.googleapis.com", "--project", "p")]


def test_the_organization_is_read_over_rest(monkeypatch):
    sent = []

    def read(url, data=None):
        sent.append((url, data))
        if url.endswith(":getAncestry"):
            return {"ancestor": [{"resourceId": {"type": "project", "id": "p"}},
                                 {"resourceId": {"type": "organization", "id": "1"}}]}
        return {"displayName": "example.com", "owner": {"directoryCustomerId": "C1"}}

    monkeypatch.setattr(gc, "api_json", read)
    monkeypatch.setattr(gweb, "directory_domains", lambda customer: {"second.example"})
    monkeypatch.setattr(gc, "gcloud", lambda *args, **kwargs: pytest.fail(args))
    assert gweb.organization("p") == gweb.Organization(
        "example.com", {"example.com", "second.example"}, True)
    assert sent == [
        ("https://cloudresourcemanager.googleapis.com/v1/projects/p:getAncestry", b"{}"),
        ("https://cloudresourcemanager.googleapis.com/v1/organizations/1", None)]


def test_a_project_with_no_organization_or_no_access(monkeypatch):
    monkeypatch.setattr(gc, "api_json", lambda url, data=None: {
        "ancestor": [{"resourceId": {"type": "project", "id": "p"}}]})
    assert gweb.organization("p") == gweb.Organization("", set(), True)
    monkeypatch.setattr(gc, "api_json", lambda url, data=None: None)
    assert gweb.organization("p") is None
    monkeypatch.setattr(gc, "gcloud", lambda *args, **kwargs: subprocess.CompletedProcess(
        args, 1, "", "PERMISSION_DENIED"))

    def no_api(url, data=None):
        raise gc.ApiReadError(url)

    monkeypatch.setattr(gc, "api_json", no_api)
    assert gweb.organization("p") is None


@pytest.mark.parametrize("module,call", [
    (gweb, lambda: gweb.directory_domains("C1")),
    (gweb, lambda: gweb.ensure_iap_agent("p", "123")),
    (gweb, lambda: gweb.hourly("p", "pdt-web", "request_count", datetime.datetime(2026, 1, 1),
                               datetime.datetime(2026, 1, 2), "ya29.token")),
    (gweb, lambda: gweb.registry_bytes("p", "us-central1", "pdt")),
    (gc, lambda: gc.billing_list("services", "services", "p")),
])
def test_every_request_with_the_token_follows_redirects_only_to_google(monkeypatch, module, call):
    openers = []

    def fetch_json(request, timeout=60, open_url=None):
        assert request.get_header("Authorization") == "Bearer ya29.token"
        openers.append(open_url)
        return {}

    monkeypatch.setattr(module, "fetch_json", fetch_json)
    monkeypatch.setattr(gc, "run_quiet", lambda *args, **kwargs: "ya29.token\n")
    monkeypatch.setattr(gc, "ACCESS_TOKEN", ("ya29.token", time.monotonic()))
    call()
    assert openers and all(opener == gc.API_OPENER.open for opener in openers)


def test_the_api_opener_refuses_a_redirect_to_another_host():
    handlers = [handler for handler in gc.API_OPENER.handlers
                if isinstance(handler, urllib.request.HTTPRedirectHandler)]
    assert [type(handler) for handler in handlers] == [gc.GoogleRedirects]
