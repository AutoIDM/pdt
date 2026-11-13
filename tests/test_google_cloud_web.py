"""The web profile on Google Cloud: a Cloud Run service behind IAP, with mounted read folders.

No test here needs the network or gcloud: every gcloud call is patched and recorded.
"""

import json
import subprocess
import urllib.error
from datetime import datetime, timezone

import pytest

from conftest import add_app, built_image
from pdt import config, deploy_google_cloud as gc, deploy_google_cloud_web as gweb
from pdt.deploy_common import BUILD_TAG

PROJECT = "my-project"
REGION = "us-central1"
NUMBER = "123456789"
WEB_APP = """\
app_profile: web
web:
  port: 8050
  health_path: /healthz
  auth:
    provider: google
    allowed_domains: [example.com]
    allowed_groups: [sales@example.com]
storage:
  read: [warehouse]
env:
  required: [TOKEN]
"""
IMAGE = f"{REGION}-docker.pkg.dev/{PROJECT}/pdt/dashboard:latest"
AGENT = f"service-{NUMBER}@gcp-sa-iap.iam.gserviceaccount.com"
SA = f"pdt-web-dashboard@{PROJECT}.iam.gserviceaccount.com"


class FakeCloud:
    """Every gcloud call deploy and destroy make, recorded; `found` answers a describe."""

    def __init__(self, found=None, pushed=(), members=()):
        self.found = found or {}
        self.pushed = set(pushed)
        self.members = list(members)
        self.calls = []
        self.apis = ()

    def run_quiet(self, *args, data=None, slow=""):
        self.calls.append(args)
        if args[:2] == ("projects", "describe"):
            return f"{NUMBER}\n"
        if args[:2] == ("auth", "print-access-token"):
            return "ya29.token\n"
        return ""

    def answer(self, args):
        for prefix, found in self.found.items():
            if args[:len(prefix)] == prefix:
                return found
        if args[:3] == ("iap", "web", "get-iam-policy"):
            return {"bindings": [{"role": gweb.IAP_ROLE, "members": self.members}]}
        return None

    def read_json_or_none(self, *args):
        self.calls.append(args)
        return self.answer(args)

    def describe_json(self, *args):
        self.calls.append(args)
        return self.answer(args)

    def pushed_image(self, image, project):
        return f"{image.rsplit(':', 1)[0]}@sha256:{'ab' * 4}" if image in self.pushed else None

    def ran(self, *prefix):
        return [call for call in self.calls if call[:len(prefix)] == prefix]


class FakeStore:
    url = f"gs://pdt-data-x/dashboard/"

    def usage(self):
        return (0, 0)


@pytest.fixture
def google(project, monkeypatch):
    (project / "pdt.yml").write_text(
        f"platform:\n  provider: google-cloud\n  project: {PROJECT}\n  region: {REGION}\n")
    add_app(project, "warehouse", "schedule: hourly\n")
    monkeypatch.setenv("TOKEN", "secret")
    monkeypatch.delenv("PDT_ARTIFACT_REGISTRY_REPO", raising=False)
    monkeypatch.delenv("PDT_CLOUD_RUN_SERVICE_ACCOUNT", raising=False)
    return project


def fake_cloud(monkeypatch, fake: FakeCloud, secrets=None):
    monkeypatch.setattr(gc, "run_quiet", fake.run_quiet)
    monkeypatch.setattr(gc, "read_json_or_none", fake.read_json_or_none)
    monkeypatch.setattr(gc, "describe_json", fake.describe_json)
    monkeypatch.setattr(gc, "pushed_image", fake.pushed_image)
    monkeypatch.setattr(gc, "preflight", lambda app, project, assume_yes: PROJECT)
    monkeypatch.setattr(gc, "ensure_apis", lambda project, yes, apis: setattr(fake, "apis", apis))
    monkeypatch.setattr(gc, "service_account_or_none", lambda project, email: None)
    monkeypatch.setattr(gc, "deployer_store", lambda project, name: FakeStore())
    monkeypatch.setattr(gc, "build_image", lambda stage, image, region:
                        fake.calls.append(("docker", "build", image)) or fake.pushed.add(image))
    monkeypatch.setattr(gc, "list_json", lambda *args: [])
    monkeypatch.setattr(gweb, "build_in_cloud", lambda app, builder, platform: False)
    monkeypatch.setattr(gweb, "ensure_image", built_image)
    monkeypatch.setattr(gweb, "gather_secrets", lambda app: {"TOKEN": "secret"}
                        if secrets is None else secrets)
    monkeypatch.setattr(gweb, "confirm", lambda actions, assume_yes, cost=None: True)
    monkeypatch.setattr(gweb, "fetch_json", lambda request: {"email": AGENT})
    monkeypatch.setattr(gweb.regions, "local_currency", lambda: "USD")
    monkeypatch.setattr(gweb, "CATALOG_WAITS", ())
    monkeypatch.setattr(gweb, "organization",
                        lambda project: gweb.Organization("example.com", {"example.com"}, True))
    return fake


def web_app(project, text=WEB_APP):
    add_app(project, "dashboard", text)
    return config.merged_app("dashboard")


def flag(args, name):
    return args[args.index(name) + 1]


def env_of(value: str) -> dict[str, str]:
    assert value.startswith("^|^")
    return dict(part.split("=", 1) for part in value[3:].split("|"))


def test_deploy_builds_the_service_behind_iap_with_a_mounted_read_folder(google, monkeypatch, capsys):
    fake = fake_cloud(monkeypatch, FakeCloud())
    app = web_app(google)
    assert gweb.deploy(app, True) == 0
    assert gweb.IAP_API in fake.apis and gweb.RESOURCE_MANAGER_API in fake.apis
    assert "cloudscheduler.googleapis.com" not in fake.apis
    [deploy] = fake.ran("run", "deploy")
    assert deploy[2] == "pdt-dashboard"
    assert "--iap" in deploy and "--no-allow-unauthenticated" in deploy
    assert "--container" not in deploy and "--depends-on" not in deploy
    assert flag(deploy, "--execution-environment") == "gen2"
    assert flag(deploy, "--ingress") == "all" and flag(deploy, "--min-instances") == "0"
    assert flag(deploy, "--labels").startswith(f"managed-by=pdt,{BUILD_TAG}=")
    assert "--clear-volumes" in deploy and "--clear-volume-mounts" in deploy
    volume = flag(deploy, "--add-volume")
    assert volume.startswith("name=read-warehouse,type=cloud-storage,bucket=pdt-data-")
    assert volume.endswith(",readonly=true,mount-options=only-dir=warehouse")
    assert flag(deploy, "--add-volume-mount") == "volume=read-warehouse,mount-path=/pdt/storage/warehouse"
    assert flag(deploy, "--port") == "8050"
    assert flag(deploy, "--image").startswith(IMAGE.rsplit(":", 1)[0] + "@sha256:")
    assert flag(deploy, "--startup-probe") == (
        "httpGet.path=/healthz,httpGet.port=8050,periodSeconds=5,timeoutSeconds=5,failureThreshold=18")
    # The service runs as its own account, never as the jobs' pdt-runner.
    assert flag(deploy, "--service-account") == SA
    [create] = fake.ran("iam", "service-accounts", "create")
    assert create[3] == "pdt-web-dashboard" and flag(create, "--display-name") == "pdt web app dashboard"
    grants = fake.ran("storage", "managed-folders", "add-iam-policy-binding")
    grants += fake.ran("secrets", "add-iam-policy-binding")
    assert grants and all(flag(c, "--member") == f"serviceAccount:{SA}" for c in grants)
    assert flag(deploy, "--set-secrets") == "PDT_ENV_JSON=pdt-dashboard-env:latest"
    env = env_of(flag(deploy, "--set-env-vars"))
    assert env["PDT_PORT"] == "8050"
    assert env["PDT_URL"] == f"https://pdt-dashboard-{NUMBER}.{REGION}.run.app/"
    assert env["PDT_STORAGE_READ_DIR"] == "/pdt/storage"
    [(other, folder)] = json.loads(env["PDT_STORAGE_READ_URLS"]).items()
    assert other == "warehouse" and folder.startswith("gs://pdt-data-") and folder.endswith("/warehouse/")
    assert env["PDT_ENV_SECRET_RESOURCE"] == f"projects/{PROJECT}/secrets/pdt-dashboard-env"
    assert "PDT_STORAGE_URL" in env
    # Only the app's own image is built.
    assert [call[2] for call in fake.ran("docker", "build")] == [IMAGE]
    # The read grant on the other app's folder, and the write grant on its own.
    reads = [c for c in fake.ran("storage", "managed-folders", "add-iam-policy-binding")
             if "/warehouse/" in c[3]]
    assert reads and flag(reads[0], "--role") == gweb.READ_ROLE
    # pdt changes no project-wide policy.
    assert fake.ran("projects", "set-iam-policy") == []
    # IAP: the agent may call the service, and the two members may enter.
    [invoker] = fake.ran("run", "services", "add-iam-policy-binding")
    assert flag(invoker, "--member") == f"serviceAccount:{AGENT}"
    adds = fake.ran("iap", "web", "add-iam-policy-binding")
    assert [flag(c, "--member") for c in adds] == ["domain:example.com", "group:sales@example.com"]
    assert all(flag(c, "--role") == gweb.IAP_ROLE and "--resource-type=cloud-run" in c
               for c in adds)
    assert fake.ran("iap", "web", "remove-iam-policy-binding") == []
    out = " ".join(capsys.readouterr().out.split())
    assert f"Deployed dashboard: https://pdt-dashboard-{NUMBER}.{REGION}.run.app/" in out


def test_a_service_with_the_old_sidecar_must_be_destroyed_first(google, monkeypatch, capsys):
    found = {("run", "services", "describe"): {
        "metadata": {"labels": {"managed-by": "pdt"}},
        "spec": {"template": {"spec": {"containers": [{"name": "app"}, {"name": "storage-sync"}]}}}}}
    fake_cloud(monkeypatch, FakeCloud(found=found))
    with pytest.raises(SystemExit):
        gweb.deploy(web_app(google), True)
    assert "pdt destroy dashboard" in capsys.readouterr().out


def test_web_resources_size_the_service(google, monkeypatch, capsys):
    fake = fake_cloud(monkeypatch, FakeCloud())
    text = WEB_APP.replace("web:\n", "web:\n  resources:\n    cpu: 2\n    memory: 2GiB\n"
                                     "    timeout: 900\n    min_instances: 1\n"
                                     "    max_instances: 5\n    concurrency: 20\n", 1)
    assert gweb.deploy(web_app(google, text), True, plan_only=True) == 0
    assert gweb.deploy(web_app(google, text), True) == 0
    [deploy] = fake.ran("run", "deploy")
    assert (flag(deploy, "--cpu"), flag(deploy, "--memory"), flag(deploy, "--timeout")) == \
        ("2", "2048Mi", "900")
    assert (flag(deploy, "--min-instances"), flag(deploy, "--max-instances"),
            flag(deploy, "--concurrency")) == ("1", "5", "20")
    out = " ".join(capsys.readouterr().out.split())
    assert "2 vCPU, 2GiB, 1 to 5 instances, 20 requests each, 900 s timeout" in out


def test_without_web_resources_the_defaults_apply(google, monkeypatch):
    fake = fake_cloud(monkeypatch, FakeCloud())
    assert gweb.deploy(web_app(google), True) == 0
    [deploy] = fake.ran("run", "deploy")
    assert (flag(deploy, "--cpu"), flag(deploy, "--memory"), flag(deploy, "--max-instances"),
            flag(deploy, "--concurrency"), flag(deploy, "--timeout")) == \
        ("1", "512Mi", "3", "80", "300")
    assert "--iap" in deploy and "--no-allow-unauthenticated" in deploy


def test_a_second_deploy_fixes_the_members(google, monkeypatch, capsys):
    found = {("run", "services", "describe"): {
        "metadata": {"labels": {"managed-by": "pdt", BUILD_TAG: "f" * 32}},
        "status": {"url": f"https://pdt-dashboard-{NUMBER}.{REGION}.run.app"}}}
    fake = fake_cloud(monkeypatch, FakeCloud(found=found, members=[
        "domain:old.example.com", "group:sales@example.com", "user:ann@example.com"]))
    assert gweb.deploy(web_app(google), True, plan_only=True) == 0
    assert gweb.deploy(web_app(google), True) == 0
    adds = fake.ran("iap", "web", "add-iam-policy-binding")
    removes = fake.ran("iap", "web", "remove-iam-policy-binding")
    assert [flag(c, "--member") for c in adds] == ["domain:example.com"]
    assert [flag(c, "--member") for c in removes] == ["domain:old.example.com"]
    out = " ".join(capsys.readouterr().out.split())
    assert "update Cloud Run service pdt-dashboard" in out


def test_a_paused_app_deploys_stopped(google, monkeypatch, capsys):
    fake = fake_cloud(monkeypatch, FakeCloud())
    assert gweb.deploy(web_app(google, WEB_APP + "pause: true\n"), True) == 0
    [deploy] = fake.ran("run", "deploy")
    assert flag(deploy, "--scaling") == "0" and flag(deploy, "--ingress") == "all"
    assert "pdt unpause dashboard" in " ".join(capsys.readouterr().out.split())


def test_the_plan_only_reads(google, monkeypatch, capsys):
    fake = fake_cloud(monkeypatch, FakeCloud())
    monkeypatch.setattr(gweb, "confirm", lambda *a, **k: pytest.fail("asked"))
    assert gweb.deploy(web_app(google), True, plan_only=True) == 0
    assert fake.ran("run", "deploy") == [] and fake.ran("docker") == []
    out = " ".join(capsys.readouterr().out.split())
    assert "Identity-Aware Proxy in front of" in out
    assert "allow domain:example.com to enter" in out
    assert "mounted read-only at /pdt/storage/warehouse/" in out


def test_iap_changes_keep_members_added_by_hand():
    add, remove = gweb.iap_changes(
        ["domain:old.example.com", "user:ann@example.com", "group:g@example.com"],
        ["domain:new.example.com", "group:g@example.com"])
    assert add == ["domain:new.example.com"]
    assert remove == ["domain:old.example.com"]


def test_the_url_and_the_env_flag():
    assert gweb.service_url("dashboard", NUMBER, REGION) == \
        f"https://pdt-dashboard-{NUMBER}.{REGION}.run.app/"
    value = gweb.env_flag([("A", "x,y"), ("B", '{"k": "v=1"}')])
    assert env_of(value) == {"A": "x,y", "B": '{"k": "v=1"}'}


def test_pause_stops_the_service_and_unpause_starts_it(google, monkeypatch, capsys):
    found = {("run", "services", "describe"): {"metadata": {"labels": {"managed-by": "pdt"}}}}
    fake = fake_cloud(monkeypatch, FakeCloud(found=found))
    app = web_app(google)
    assert gweb.pause(app, True, True) == 0
    assert gweb.pause(app, False, True) == 0
    updates = fake.ran("run", "services", "update")
    assert [flag(c, "--scaling") for c in updates] == ["0", "auto"]
    out = " ".join(capsys.readouterr().out.split())
    assert "Paused dashboard" in out and "Unpaused dashboard" in out


def test_pause_of_a_missing_service_says_deploy_first(google, monkeypatch, capsys):
    fake_cloud(monkeypatch, FakeCloud())
    with pytest.raises(SystemExit):
        gweb.pause(web_app(google), True, True)
    assert "pdt deploy dashboard" in capsys.readouterr().out


def test_run_deployed_opens_the_deployed_url(google, monkeypatch, capsys):
    fake_cloud(monkeypatch, FakeCloud())
    opened = []
    monkeypatch.setattr(gweb.webbrowser, "open", opened.append)
    assert gweb.start(web_app(google), True) == 0
    assert opened == [f"https://pdt-dashboard-{NUMBER}.{REGION}.run.app/"]


def test_main_hands_a_web_app_to_the_web_module(google, monkeypatch):
    web_app(google)
    calls = []
    monkeypatch.setattr(gc.sys, "argv", ["deploy_google_cloud.py", "pause", "dashboard"])
    monkeypatch.setattr(gweb, "command",
                        lambda app, name, rest, yes, plan: calls.append((app["name"], name)) or 0)
    assert gc.main() == 0
    assert calls == [("dashboard", "pause")]


def test_main_keeps_a_job_on_the_job_path(google, monkeypatch):
    monkeypatch.setattr(gc.sys, "argv", ["deploy_google_cloud.py", "pause", "warehouse"])
    monkeypatch.setattr(gweb, "command", lambda *a: pytest.fail("web module"))
    monkeypatch.setattr(gc, "pause", lambda app, paused, yes: 7)
    assert gc.main() == 7


def test_the_deployed_service_is_one_run(monkeypatch):
    service = {"metadata": {"creationTimestamp": "2026-11-01T10:00:00Z"},
               "status": {"conditions": [{"type": "Ready", "status": "True",
                                          "lastTransitionTime": "2026-11-12T10:00:00Z"}]}}
    revision = {"metadata": {"creationTimestamp": "2026-11-12T10:00:00Z"}}
    service["status"]["latestCreatedRevisionName"] = "pdt-dashboard-00002-abc"
    monkeypatch.setattr(gc, "read_json_or_none", lambda *args:
                        revision if args[:2] == ("run", "revisions") else service)
    [run] = gweb.deployed_runs(PROJECT, REGION, "pdt-dashboard")
    assert run.id == "pdt-dashboard" and run.status == "running" and run.ended is None
    assert run.started == datetime(2026, 11, 12, 10, tzinfo=timezone.utc)
    service["metadata"]["annotations"] = {gweb.SCALING_MODE: "manual", gweb.MANUAL_COUNT: "0"}
    [run] = gweb.deployed_runs(PROJECT, REGION, "pdt-dashboard")
    assert run.status == "stopped"
    monkeypatch.setattr(gc, "read_json_or_none", lambda *args: None)
    assert gweb.deployed_runs(PROJECT, REGION, "pdt-dashboard") == []


def test_read_lines_reads_the_output_and_both_system_logs_of_the_service(monkeypatch):
    seen = []
    output = [{"timestamp": "2026-11-12T10:00:00Z", "textPayload": "Starting new instance",
               "logName": "projects/p/logs/run.googleapis.com%2F%2Fvar%2Flog%2Fsystem"},
              {"timestamp": "2026-11-12T10:00:01Z", "textPayload": "Probe passed",
               "logName": "projects/p/logs/run.googleapis.com%2Fvarlog%2Fsystem"},
              {"timestamp": "2026-11-12T10:00:02Z", "textPayload": "GET / 200",
               "logName": "projects/p/logs/run.googleapis.com%2Fstderr"}]

    def gcloud(*args):
        seen.append(args)
        return subprocess.CompletedProcess(args, 0, json.dumps(output), "")

    monkeypatch.setattr(gc, "gcloud", gcloud)
    lines = gweb.read_lines(PROJECT, "pdt-dashboard")
    assert [line.message for line in lines] == [
        "[system] Starting new instance", "[system] Probe passed", "GET / 200"]
    [query] = seen
    assert 'resource.labels.service_name="pdt-dashboard"' in query[2]
    assert "revision_name" not in query[2]
    assert 'log_id("run.googleapis.com//var/log/system")' in query[2]
    assert 'log_id("run.googleapis.com/varlog/system")' in query[2]


def test_destroy_removes_the_service_the_secret_the_image_and_the_read_grant(google, monkeypatch,
                                                                             capsys):
    managed = {"metadata": {"labels": {"managed-by": "pdt"}}}
    found = {("run", "services", "describe"): managed,
             ("secrets", "describe"): {"labels": {"managed-by": "pdt"}},
             ("artifacts", "repositories", "describe"): {"labels": {"managed-by": "pdt"}},
             ("storage", "buckets", "describe"): {"labels": {"managed-by": "pdt"}},
             ("storage", "managed-folders", "get-iam-policy"): {"bindings": [
                 {"role": gweb.READ_ROLE, "members": [f"serviceAccount:{SA}"]},
                 {"role": gc.STORE_ROLE, "members": [f"serviceAccount:{SA}"]}]}}
    fake = fake_cloud(monkeypatch, FakeCloud(found=found))
    monkeypatch.setattr(gc, "list_json", lambda *args: (
        [{"name": "warehouse/"}, {"name": "dashboard/"}] if args[1] == "managed-folders"
        else [{"package": f"x/{PROJECT}/pdt/dashboard"}]))
    monkeypatch.setattr(gweb, "warn_if_locked", lambda store, name: None)
    monkeypatch.setattr(gc, "service_account_or_none", lambda project, email:
                        {"email": email, "displayName": "pdt web app dashboard"})
    assert gweb.destroy(web_app(google), True) == 0
    [delete] = fake.ran("iam", "service-accounts", "delete")
    assert delete[3] == SA
    # The account goes after every grant it held is removed.
    revokes = [fake.calls.index(c) for c in fake.ran("storage", "managed-folders",
                                                      "remove-iam-policy-binding")]
    assert revokes and fake.calls.index(delete) > max(revokes)
    assert [c[:3] for c in fake.ran("run", "services", "delete")] == [("run", "services", "delete")]
    assert fake.ran("secrets", "delete") != []
    # The app's image was the last in the repository, so the repository goes with it.
    assert fake.ran("artifacts", "docker", "images", "delete") == []
    assert fake.ran("artifacts", "repositories", "delete") != []
    removes = fake.ran("storage", "managed-folders", "remove-iam-policy-binding")
    assert any("/warehouse/" in c[3] and flag(c, "--role") == gweb.READ_ROLE for c in removes)
    assert any("/dashboard/" in c[3] and flag(c, "--role") == gc.STORE_ROLE for c in removes)
    out = " ".join(capsys.readouterr().out.split())
    assert "Removed dashboard from project my-project" in out
    assert "bucket pdt-data-" in out


def test_an_account_of_the_same_name_not_made_by_pdt_stops_the_deploy(google, monkeypatch,
                                                                       capsys):
    fake = fake_cloud(monkeypatch, FakeCloud())
    monkeypatch.setattr(gc, "service_account_or_none", lambda project, email:
                        {"email": email, "displayName": "someone else's"})
    with pytest.raises(SystemExit):
        gweb.deploy(web_app(google), True)
    assert fake.ran("run", "deploy") == []
    assert "not managed by PDT" in capsys.readouterr().out


def test_a_long_app_name_gets_an_account_id_of_30_characters():
    assert gweb.service_account_id("dashboard") == "pdt-web-dashboard"
    long_a = gweb.service_account_id("quarterly-revenue-dashboard-east")
    long_b = gweb.service_account_id("quarterly-revenue-dashboard-west")
    assert len(long_a) == 30 and long_a.startswith("pdt-web-quarterly-revenue")
    assert long_a != long_b


def test_destroy_with_nothing_deployed_removes_nothing(google, monkeypatch, capsys):
    fake = fake_cloud(monkeypatch, FakeCloud())
    assert gweb.destroy(web_app(google), True) == 0
    assert fake.ran("run", "services", "delete") == []
    assert "Nothing to remove for dashboard" in " ".join(capsys.readouterr().out.split())


PRICES = {gweb.CPU_SKU: 0.000024, gweb.MEMORY_SKU: 0.0000025, gweb.MIN_CPU_SKU: 0.0000025,
          gweb.MIN_MEMORY_SKU: 0.0000025, gweb.REQUESTS_SKU: 0.0000004, gweb.REGISTRY_SKU: 0.10}
SECOND = 1 * 0.000024 + 0.5 * 0.0000025


def priced(monkeypatch):
    monkeypatch.setattr(gweb, "cached_price", lambda provider, region, currency, query, read, *a:
                        (PRICES[query.split(": ", 1)[1]], "s"))
    monkeypatch.setattr(gweb.regions, "local_currency", lambda: "USD")


def test_a_new_app_is_priced_from_the_default_visits(monkeypatch):
    priced(monkeypatch)
    app = {"auth": {"provider": "google"}, "web": {}}
    cost = gweb.cost_estimate(app, "p", "us-central1", None, int(0.8 * 1024 ** 3))
    labels = dict(cost.items)
    [(label, amount)] = [item for item in cost.items if item[0].startswith("Cloud Run:")]
    assert label == "Cloud Run: 53 min a month (new app)"
    assert round(amount, 4) == round(7 * (20 * 21.7 + 2 * 8.7) * SECOND, 4)
    assert round(labels["Artifact Registry: 0.80 GiB of images, shared (0.5 GiB free)"], 2) == 0.03
    assert labels["Identity-Aware Proxy"] == 0.0
    assert not any("idle" in label for label in labels)


def test_a_deployed_app_is_priced_from_its_measured_use(monkeypatch):
    priced(monkeypatch)
    use = gweb.Use(weekday_seconds=83.5, weekday_requests=30, weekdays=1.0)
    app = {"auth": {"provider": "google"}, "web": {"resources": {"min_instances": 1}}}
    cost = gweb.cost_estimate(app, "p", "us-central1", use)
    [(label, amount)] = [item for item in cost.items if item[0].startswith("Cloud Run:")]
    assert label == "Cloud Run: 31 min a month (measured)"
    assert round(amount, 4) == round((83.5 * 21.7 + 8.35 * 8.7) * SECOND, 4)
    idle = dict(cost.items)["1 idle instance(s) kept running"]
    assert round(idle, 2) == round(730 * 3600 * (0.0000025 + 0.5 * 0.0000025), 2)



def test_a_catalog_that_fails_once_is_read_again(monkeypatch):
    priced(monkeypatch)
    real, waits = gweb.cached_price, []
    calls = iter([LookupError("busy")])

    def flaky(*args):
        for error in calls:
            raise error
        return real(*args)
    monkeypatch.setattr(gweb, "cached_price", flaky)
    monkeypatch.setattr(gweb.time, "sleep", waits.append)
    app = {"auth": {"provider": "google"}, "web": {}}
    assert gweb.cost_estimate(app, "p", "us-central1") is not None
    assert waits == [0, gweb.CATALOG_WAITS[0]]


def test_a_catalog_that_never_answers_gives_no_estimate(monkeypatch, capsys):
    priced(monkeypatch)
    waits = []

    def refused(*args):
        raise LookupError("no answer")
    monkeypatch.setattr(gweb, "cached_price", refused)
    monkeypatch.setattr(gweb.time, "sleep", waits.append)
    app = {"auth": {"provider": "google"}, "web": {}}
    assert gweb.cost_estimate(app, "p", "us-central1") is None
    assert waits == [0, *gweb.CATALOG_WAITS]
    assert "No cost estimate" in capsys.readouterr().out

def test_measured_use_splits_weekdays_from_weekend_days(monkeypatch):
    from datetime import datetime, timezone
    monkeypatch.setattr(gc, "run_quiet", lambda *a, **k: "token")
    friday = datetime(2026, 11, 13, 12, tzinfo=timezone.utc)
    sums = {"container/billable_instance_time": {datetime(2026, 11, 13, 13, tzinfo=timezone.utc): 10.0,
                                                  datetime(2026, 11, 14, 10, tzinfo=timezone.utc): 4.0},
            "request_count": {datetime(2026, 11, 13, 13, tzinfo=timezone.utc): 3.0}}
    monkeypatch.setattr(gweb, "hourly", lambda project, service, metric, start, end, token:
                        sums[metric])
    use = gweb.measured_use("p", "pdt-dashboard", friday,
                            now=datetime(2026, 11, 15, 0, tzinfo=timezone.utc))
    assert (use.weekday_seconds, use.weekday_requests, use.weekend_seconds) == (10.0, 3.0, 4.0)
    assert round(use.weekdays, 3) == 0.5 and round(use.weekend_days, 3) == 1.0


def test_a_deploy_sets_the_clean_up_policy_once(google, monkeypatch):
    fake = fake_cloud(monkeypatch, FakeCloud())
    assert gweb.deploy(web_app(google), True) == 0
    [policy] = fake.ran("artifacts", "repositories", "set-cleanup-policies")
    assert "--no-dry-run" in policy
    policies = {name: {**policy, "id": name} for name, policy in gc.CLEANUP_POLICIES_SET.items()}
    repository = {"labels": {"managed-by": "pdt"}, "cleanupPolicies": policies}
    fake = fake_cloud(monkeypatch, FakeCloud(found={("artifacts", "repositories", "describe"):
                                                    repository}))
    assert gweb.deploy(web_app(google), True) == 0
    assert fake.ran("artifacts", "repositories", "set-cleanup-policies") == []


def test_a_redeploy_removes_grants_the_config_no_longer_names(google, monkeypatch, capsys):
    found = {("storage", "buckets", "describe"): {"labels": {"managed-by": "pdt"}},
             ("storage", "managed-folders", "get-iam-policy"): {"bindings": [
                 {"role": gweb.READ_ROLE, "members": [f"serviceAccount:{SA}"]},
                 {"role": gc.STORE_ROLE, "members": [f"serviceAccount:{SA}"]}]}}
    fake = fake_cloud(monkeypatch, FakeCloud(found=found))
    monkeypatch.setattr(gc, "list_json", lambda *args: (
        [{"name": "warehouse/"}, {"name": "old-source/"}, {"name": "dashboard/"}]
        if args[1] == "managed-folders" else []))
    text = WEB_APP.replace("storage:\n  read: [warehouse]\n",
                           "storage:\n  enabled: false\n  read: [warehouse]\n")
    assert gweb.deploy(web_app(google, text), True) == 0
    removes = fake.ran("storage", "managed-folders", "remove-iam-policy-binding")
    assert [(c[3].rsplit("/", 2)[-2], flag(c, "--role")) for c in removes] == [
        ("old-source", gweb.READ_ROLE), ("dashboard", gc.STORE_ROLE)]


def test_the_accounts_the_managed_sign_in_cannot_serve():
    app = {"auth": {"allowed_domains": ["example.com", "Second.example", "partner.example"]}}
    listed = gweb.Organization("example.com", {"example.com", "second.example"}, True)
    assert gweb.audience_problems(app, listed) == ([
        "partner.example is not a domain of the project's organization (example.com), so its "
        "accounts cannot sign in"], [])
    unlisted = gweb.Organization("example.com", {"example.com", "second.example"}, False)
    stops, cautions = gweb.audience_problems(app, unlisted)
    assert stops == [] and len(cautions) == 1 and cautions[0].startswith("partner.example ")
    stops, cautions = gweb.audience_problems(app, gweb.Organization("", set(), True))
    assert "no organization" in stops[0] and cautions == []
    assert gweb.audience_problems(app, None) == ([], [])


def test_the_organization_holds_every_domain_the_directory_lists(monkeypatch):
    answers = {("projects", "get-ancestors"): '[{"id": "p", "type": "project"}, '
                                              '{"id": "1", "type": "organization"}]',
               ("organizations", "describe"): '{"displayName": "example.com", '
                                              '"owner": {"directoryCustomerId": "C1"}}',
               ("auth", "list"): "deployer@second.example\n"}
    monkeypatch.setattr(gc, "gcloud", lambda *args, **kw: subprocess.CompletedProcess(
        args, 0, answers[args[:2]], ""))
    monkeypatch.setattr(gc, "run_quiet", lambda *args: "token")
    monkeypatch.setattr(gweb, "fetch_json", lambda request: {"domains": [
        {"domainName": "example.com"}, {"domainName": "Third.example"}]})
    assert gweb.organization("p") == gweb.Organization(
        "example.com", {"example.com", "third.example"}, True)

    def refused(request):
        raise urllib.error.HTTPError(request.full_url, 403, "scope", {}, None)
    monkeypatch.setattr(gweb, "fetch_json", refused)
    assert gweb.organization("p") == gweb.Organization(
        "example.com", {"example.com", "second.example"}, False)


def test_a_domain_outside_the_organization_stops_the_deploy_before_any_change(google, monkeypatch,
                                                                            capsys):
    fake = fake_cloud(monkeypatch, FakeCloud())
    monkeypatch.setattr(gweb, "organization",
                        lambda project: gweb.Organization("other.example", {"other.example"}, True))
    with pytest.raises(SystemExit):
        gweb.deploy(web_app(google), True)
    assert fake.ran("iam", "service-accounts", "create") == []
    assert fake.ran("run", "deploy") == []
    assert "example.com is not a domain of the project's organization" in capsys.readouterr().out

def test_a_clean_up_policy_with_the_right_names_but_other_rules_is_set_again():
    policies = {name: {**policy, "id": name} for name, policy in gc.CLEANUP_POLICIES_SET.items()}
    assert gc.cleanup_policies_set({"cleanupPolicies": policies})
    policies["pdt-keep-newest"]["mostRecentVersions"] = {"keepCount": 1}
    assert not gc.cleanup_policies_set({"cleanupPolicies": policies})


def test_a_web_app_deployed_earlier_as_a_job_must_be_destroyed_as_a_job_first(google, monkeypatch,
                                                                              capsys):
    found = {("run", "jobs", "describe"): {"metadata": {"labels": {"managed-by": "pdt"}}}}
    fake = fake_cloud(monkeypatch, FakeCloud(found=found))
    with pytest.raises(SystemExit):
        gweb.deploy(web_app(google), True)
    assert fake.ran("run", "deploy") == []
    out = " ".join(capsys.readouterr().out.split())
    assert "Cloud Run job pdt-dashboard is still deployed" in out
    assert "set app_profile: job and run pdt destroy dashboard" in out
    with pytest.raises(SystemExit):
        gweb.destroy(web_app(google), True)
    assert fake.ran("run", "services", "delete") == []
