import json
import re
import subprocess
from pathlib import Path

import pytest

from conftest import add_app, plain
from pdt import deploy_google_cloud as google
from pdt.deploy_common import BUILD_TAG, LIFECYCLE_MODULES, CostEstimate, store_suffix

IMAGE = "us-central1-docker.pkg.dev/my-project/pdt/report:latest"
BUILDER = "pdt-builder@my-project.iam.gserviceaccount.com"
BUILDS = f"pdt-build-{store_suffix('my-project')}"


def pinned(digest):
    return f"{IMAGE.rsplit(':', 1)[0]}@{digest}"


def fake_build(monkeypatch, tmp_path, build):
    stage = tmp_path / "stage"
    stage.mkdir()
    monkeypatch.setattr(google, "run_quiet", lambda *args: "ya29.token\n"
                        if args == ("auth", "print-access-token") else pytest.fail(args))
    monkeypatch.setattr(google, "run_build", build)
    return stage


def test_logs_in_builds_for_amd64_and_pushes_to_artifact_registry(tmp_path, monkeypatch):
    calls = []
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    stage = fake_build(monkeypatch, tmp_path,
                       lambda command, data=None, **_: calls.append((command, data)))
    google.build_image(stage, IMAGE, "us-central1")
    assert calls == [
        (["docker", "login", "--username", "oauth2accesstoken", "--password-stdin",
          "https://us-central1-docker.pkg.dev"], "ya29.token"),
        (["docker", "build", "--platform", "linux/amd64", "-t", IMAGE, str(stage)], None),
        (["docker", "push", IMAGE], None),
    ]


def test_a_failed_docker_build_does_not_push(tmp_path, monkeypatch):
    calls = []

    def build(command, data=None, **_):
        calls.append(command[:2])
        if command[1] == "build":
            raise SystemExit(1)

    stage = fake_build(monkeypatch, tmp_path, build)
    with pytest.raises(SystemExit):
        google.build_image(stage, IMAGE, "us-central1")
    assert calls == [["docker", "login"], ["docker", "build"]]


class FakeGcloud:
    """gcloud as deploy and destroy see it: `found` maps a command prefix to its json."""

    def __init__(self, found=None, enabled=(*google.APIS, google.CLOUD_BUILD_API),
                 submit_code=0, submit_stderr="", digest="sha256:new"):
        self.found = found or {}
        self.digest = digest
        self.enabled = enabled
        self.submit_code = submit_code
        self.submit_stderr = submit_stderr
        self.calls = []
        self.uploaded = None
        self.build_config = None
        self.ignore = None

    def __call__(self, command, *args, **kwargs):
        if command[0] != google.GCLOUD:
            pytest.fail(f"ran {command}")
        args = list(command[1:])
        self.calls.append(args)
        for prefix, answer in self.found.items():
            if args[:len(prefix)] == list(prefix):
                return subprocess.CompletedProcess(command, 0, json.dumps(answer), "")
        if args[:3] == ["services", "list", "--enabled"]:
            return subprocess.CompletedProcess(command, 0, "\n".join(self.enabled), "")
        if args[:2] == ["builds", "submit"]:
            source = Path(args[2])
            self.uploaded = sorted(path.relative_to(source).as_posix()
                                   for path in source.rglob("*") if path.is_file())
            self.build_config = json.loads(Path(args[args.index("--config") + 1]).read_text())
            self.ignore = (source / ".gcloudignore").read_text()
            return subprocess.CompletedProcess(command, self.submit_code, None,
                                               self.submit_stderr)
        if args[:4] == ["artifacts", "docker", "images", "describe"] and self.digest:
            summary = {"digest": self.digest, "fully_qualified_digest": pinned(self.digest)}
            return subprocess.CompletedProcess(command, 0, json.dumps({"image_summary": summary}),
                                               "")
        if "describe" in args:
            return subprocess.CompletedProcess(command, 1, "", "ERROR: NOT_FOUND: not found")
        if "list" in args:
            return subprocess.CompletedProcess(command, 0, "[]", "")
        return subprocess.CompletedProcess(command, 0, "", "")

    def ran(self, *prefix):
        return [call for call in self.calls if call[:len(prefix)] == list(prefix)]


def deploy_app(project, monkeypatch, fake, cloud):
    folder = add_app(project, "report")
    (folder / ".env").write_text("TOKEN=secret\n")
    (folder / "id_rsa").write_text("key\n")
    (folder / ".gitignore").write_text("*.csv\n")
    (folder / "data.csv").write_text("a,b\n")
    monkeypatch.setattr(subprocess, "run", fake)
    monkeypatch.setattr(google, "build_in_cloud", lambda app, builder, platform: cloud)
    monkeypatch.setattr(google, "preflight", lambda app, project, assume_yes: "my-project")
    monkeypatch.setattr(google, "confirm", lambda actions, assume_yes, cost=None: True)
    monkeypatch.setattr(google, "cost_estimate", lambda *args, **kwargs: CostEstimate([], "", ""))
    monkeypatch.setattr(google, "gather_secrets", lambda app: {})
    monkeypatch.setattr(google.regions, "local_currency", lambda: "USD")
    monkeypatch.setattr(google, "set_scheduler_paused", lambda *args: None)
    monkeypatch.setattr(google, "run_build", lambda command, data=None: pytest.fail(command))
    monkeypatch.delenv("PDT_ARTIFACT_REGISTRY_REPO", raising=False)
    monkeypatch.delenv("PDT_CLOUD_RUN_SERVICE_ACCOUNT", raising=False)
    return {"name": "report", "dir": folder, "schedule": "daily", "timezone": "Etc/UTC",
            "pause": False, "storage": False,
            "platform": {"project": "my-project", "region": "us-central1"}}


def test_the_cloud_path_submits_the_staged_context_to_cloud_build_and_runs_no_docker(
        project, monkeypatch):
    fake = FakeGcloud()
    app = deploy_app(project, monkeypatch, fake, cloud=True)
    built = []
    monkeypatch.setattr(google, "build_image", lambda *args: built.append(args))
    assert google.deploy(app, True) == 0
    assert built == []
    [submit] = fake.ran("builds", "submit")
    assert submit[submit.index("--region") + 1] == "us-central1"
    assert submit[submit.index("--service-account") + 1] == (
        f"projects/my-project/serviceAccounts/{BUILDER}")
    assert submit[submit.index("--gcs-source-staging-dir") + 1] == f"gs://{BUILDS}/source"
    assert fake.build_config == {
        "steps": [{"name": "gcr.io/cloud-builders/docker", "env": ["DOCKER_BUILDKIT=1"],
                   "args": ["build", "--platform", "linux/amd64", "-t", IMAGE, "."]}],
        "images": [IMAGE],
        "logsBucket": f"gs://{BUILDS}/logs",
        "options": {"logging": "GCS_ONLY"},
    }
    assert fake.uploaded == sorted([
        ".gcloudignore", "Dockerfile", "cloudbuild.json", "pdt.yml", "report/data.csv",
        ".pdt-script/run.py", "report/run.py",
        *(f".pdt-lifecycle/pdt/{module}" for module in LIFECYCLE_MODULES)])
    assert fake.ignore == "/.gcloudignore\n/cloudbuild.json\n"
    assert not Path(submit[2]).exists()



def test_the_cloud_path_creates_the_builder_and_its_bucket_before_the_build(project, monkeypatch):
    fake = FakeGcloud()
    app = deploy_app(project, monkeypatch, fake, cloud=True)
    google.deploy(app, True)
    order = [call[:3] for call in fake.calls]
    [create] = fake.ran("iam", "service-accounts", "create", "pdt-builder")
    assert create[create.index("--display-name") + 1] == google.BUILDER_DISPLAY_NAME
    [bucket] = fake.ran("storage", "buckets", "create")
    assert bucket[3] == f"gs://{BUILDS}"
    for flag in ("--uniform-bucket-level-access", "--public-access-prevention"):
        assert flag in bucket
    assert bucket[bucket.index("--soft-delete-duration") + 1] == "0"
    assert ["storage", "buckets", "update", f"gs://{BUILDS}", "--update-labels",
            "managed-by=pdt"] in fake.calls
    member = f"serviceAccount:{BUILDER}"
    [push] = fake.ran("artifacts", "repositories", "add-iam-policy-binding")
    assert push[push.index("--member") + 1] == member
    assert push[push.index("--role") + 1] == "roles/artifactregistry.writer"
    [use] = fake.ran("storage", "buckets", "add-iam-policy-binding")
    assert use[3] == f"gs://{BUILDS}" and use[use.index("--member") + 1] == member
    submit_at = order.index(["builds", "submit", fake.ran("builds", "submit")[0][2]])
    assert order.index(["storage", "buckets", "create"]) < submit_at
    assert order.index(["artifacts", "repositories", "add-iam-policy-binding"]) < submit_at


def test_the_build_bucket_deletes_objects_after_one_day(project, monkeypatch):
    lifecycles = []
    fake = FakeGcloud()

    def run(command, *args, **kwargs):
        if command[1:4] == ["storage", "buckets", "create"]:
            lifecycles.append(json.loads(Path(command[command.index("--lifecycle-file") + 1])
                                         .read_text()))
        return fake(command, *args, **kwargs)

    app = deploy_app(project, monkeypatch, fake, cloud=True)
    monkeypatch.setattr(subprocess, "run", run)
    google.deploy(app, True)
    assert lifecycles == [{"rule": [{"action": {"type": "Delete"}, "condition": {"age": 1}}]}]


def test_only_the_cloud_path_enables_cloud_build(project, monkeypatch):
    fake = FakeGcloud(enabled=google.APIS)
    google.deploy(deploy_app(project, monkeypatch, fake, cloud=True), True)
    [enable] = fake.ran("services", "enable")
    assert enable[2:-2] == [google.CLOUD_BUILD_API]


def test_the_local_path_creates_no_cloud_build_resource(project, monkeypatch):
    fake = FakeGcloud(enabled=google.APIS)
    app = deploy_app(project, monkeypatch, fake, cloud=False)
    built = []
    monkeypatch.setattr(google, "build_image", lambda *args: built.append(args))
    assert google.deploy(app, True) == 0
    assert [args[1:] for args in built] == [(IMAGE, "us-central1")]
    assert fake.ran("services", "enable") == []
    assert fake.ran("builds") == []
    text = json.dumps(fake.calls)
    assert BUILDS not in text and "pdt-builder" not in text


def test_the_plan_names_the_cloud_build_resources(project, monkeypatch):
    plans = []
    fake = FakeGcloud()
    app = deploy_app(project, monkeypatch, fake, cloud=True)
    monkeypatch.setattr(google, "confirm", lambda actions, assume_yes, cost=None:
                        plans.append([str(action) for action in actions]) or True)
    google.deploy(app, True)
    plan = " | ".join(plans[-1])
    assert BUILDER in plan
    assert BUILDS in plan
    assert "in the cloud with" in plan and "Cloud Build" in plan


def test_an_unchanged_secret_lists_and_destroys_no_versions(project, monkeypatch):
    values = {"TOKEN": "secret"}
    fake = FakeGcloud(found={
        ("secrets", "describe"): {"labels": {"managed-by": "pdt"}},
        ("secrets", "versions", "access"): values,
        ("secrets", "versions", "list"): [{"name": "projects/1/secrets/s/versions/2"},
                                          {"name": "projects/1/secrets/s/versions/1"}]})
    app = deploy_app(project, monkeypatch, fake, cloud=False)
    monkeypatch.setattr(google, "gather_secrets", lambda app: values)
    monkeypatch.setattr(google, "build_image", lambda *args: None)
    assert google.deploy(app, True) == 0
    assert fake.ran("secrets", "versions", "add") == []
    assert fake.ran("secrets", "versions", "list") == []
    assert fake.ran("secrets", "versions", "destroy") == []


def test_a_failed_cloud_build_fails_once(project, monkeypatch, capsys):
    fake = FakeGcloud(submit_code=1, submit_stderr="ERROR: build step 0 failed")
    app = deploy_app(project, monkeypatch, fake, cloud=True)
    monkeypatch.setattr(google, "build_image", lambda *args: pytest.fail("built locally"))
    with pytest.raises(SystemExit):
        google.deploy(app, True)
    assert len(fake.ran("builds", "submit")) == 1
    assert fake.ran("run", "jobs", "deploy") == []
    out = capsys.readouterr().out
    assert "build step 0 failed" in out and "failed" in out


@pytest.mark.parametrize("stderr, role, where", [
    ("ERROR: (gcloud.builds.submit) PERMISSION_DENIED: caller does not have permission "
     "to act as service account", "roles/iam.serviceAccountUser", BUILDER),
    ("ERROR: (gcloud.builds.submit) PERMISSION_DENIED: The caller does not have permission "
     "'cloudbuild.builds.create'", "roles/cloudbuild.builds.editor", "my-project"),
])
def test_a_refused_submit_names_the_missing_role(project, monkeypatch, capsys, stderr, role,
                                                 where):
    fake = FakeGcloud(submit_code=1, submit_stderr=stderr)
    with pytest.raises(SystemExit):
        google.deploy(deploy_app(project, monkeypatch, fake, cloud=True), True)
    out = " ".join(capsys.readouterr().out.split())
    assert f"add the role {role} for your account on" in out
    assert where in out.split("add the role")[-1]
    assert len(fake.ran("builds", "submit")) == 1


def deployed_job(fingerprint, image):
    return {"metadata": {"labels": {"managed-by": "pdt", BUILD_TAG: fingerprint}},
            "spec": {"template": {"spec": {"template": {"spec": {
                "containers": [{"image": image}]}}}}}}


def test_the_job_runs_the_pushed_digest_and_records_the_fingerprint(project, monkeypatch):
    fake = FakeGcloud()
    assert google.deploy(deploy_app(project, monkeypatch, fake, cloud=True), True) == 0
    [deploy] = fake.ran("run", "jobs", "deploy")
    assert deploy[deploy.index("--image") + 1] == pinned("sha256:new")
    labels = deploy[deploy.index("--labels") + 1]
    assert re.fullmatch(f"managed-by=pdt,{BUILD_TAG}=[0-9a-f]{{32}}", labels)


@pytest.mark.parametrize("cloud", [True, False])
def test_an_unchanged_app_skips_the_build_and_the_builder_setup(project, monkeypatch, capsys,
                                                                cloud):
    fake = FakeGcloud()
    google.deploy(deploy_app(project, monkeypatch, fake, cloud=True), True)
    [deploy] = fake.ran("run", "jobs", "deploy")
    fingerprint = deploy[deploy.index("--labels") + 1].rsplit("=", 1)[1]
    job = deployed_job(fingerprint, pinned("sha256:new"))
    fake = FakeGcloud(found={("run", "jobs", "describe"): job})
    app = deploy_app(project, monkeypatch, fake, cloud=cloud)
    monkeypatch.setattr(google, "build_image", lambda *args: pytest.fail("built locally"))
    capsys.readouterr()
    assert google.deploy(app, True) == 0
    assert fake.ran("builds") == []
    assert fake.ran("iam", "service-accounts", "create", "pdt-builder") == []
    assert fake.ran("artifacts", "repositories", "add-iam-policy-binding") == []
    [deploy] = fake.ran("run", "jobs", "deploy")
    assert deploy[deploy.index("--image") + 1] == pinned("sha256:new")
    assert deploy[deploy.index("--labels") + 1] == f"managed-by=pdt,{BUILD_TAG}={fingerprint}"
    out = " ".join(capsys.readouterr().out.split())
    assert f"Image {IMAGE} is unchanged; skipped the build and push." in out


def test_a_changed_app_or_a_gone_image_builds(project, monkeypatch):
    for job in (deployed_job("0" * 32, pinned("sha256:new")), deployed_job("0" * 32, "x")):
        fake = FakeGcloud(found={("run", "jobs", "describe"): job})
        assert google.deploy(deploy_app(project, monkeypatch, fake, cloud=True), True) == 0
        assert len(fake.ran("builds", "submit")) == 1


def destroy_fake(builder=True, bucket=True, other_jobs=()):
    found = {("artifacts", "repositories", "describe"): {"labels": {"managed-by": "pdt"}},
             ("run", "jobs", "list"): [{"metadata": {"name": job, "labels": {
                 "managed-by": "pdt", "cloud.googleapis.com/location": "us-central1"}}}
                 for job in other_jobs],
             ("iam", "service-accounts", "list"): [
                 {"email": BUILDER, "displayName": google.BUILDER_DISPLAY_NAME}] if builder else []}
    if bucket:
        found[("storage", "buckets", "describe", f"gs://{BUILDS}")] = {
            "labels": {"managed-by": "pdt"}}
    return FakeGcloud(found=found)


def destroy_app(project, monkeypatch, fake, unreachable=()):
    app = deploy_app(project, monkeypatch, fake, cloud=False)
    monkeypatch.setattr(google, "confirm", lambda actions, assume_yes, cost=None: True)
    monkeypatch.setattr(google, "cloud_run_list", lambda project, kind: (
        fake.found.get(("run", kind, "list"), []), list(unreachable), ""))
    return app


def run_item(name, region, account, job=True):
    template = {"spec": {"serviceAccountName": account}}
    return {"metadata": {"name": name, "labels": {
                "managed-by": "pdt", "cloud.googleapis.com/location": region}},
            "spec": {"template": {"spec": {"template": template}} if job else template}}


def test_destroy_of_the_last_app_removes_the_builder_and_its_bucket(project, monkeypatch, capsys):
    fake = destroy_fake()
    assert google.destroy(destroy_app(project, monkeypatch, fake), True) == 0
    assert ["iam", "service-accounts", "delete", BUILDER, "--project", "my-project",
            "--quiet"] in fake.calls
    assert ["storage", "rm", "--recursive", f"gs://{BUILDS}/", "--project",
            "my-project"] in fake.calls
    assert "Still present" not in capsys.readouterr().out


def test_destroy_keeps_the_builder_while_another_app_runs(project, monkeypatch, capsys):
    fake = destroy_fake(other_jobs=("pdt-other",))
    google.destroy(destroy_app(project, monkeypatch, fake), True)
    assert fake.ran("iam", "service-accounts", "delete") == []
    assert fake.ran("storage", "rm") == []
    out = capsys.readouterr().out
    assert BUILDER in out and BUILDS in out


RUNNER = "pdt-runner@my-project.iam.gserviceaccount.com"
OWN = "pdt-job-report@my-project.iam.gserviceaccount.com"
SECRET_ROLES = ["roles/secretmanager.secretAccessor", "roles/secretmanager.secretVersionManager"]


def secret_fake(account=RUNNER, **kwargs):
    fake = destroy_fake(**kwargs)
    fake.found[("secrets", "describe")] = {"labels": {"managed-by": "pdt"}}
    fake.found[("secrets", "get-iam-policy")] = {"bindings": [
        {"role": role, "members": [f"serviceAccount:{account}"]} for role in SECRET_ROLES]}
    return fake


@pytest.mark.parametrize("elsewhere", [True, False])
def test_destroy_keeps_the_secret_while_the_app_runs_in_another_region(project, monkeypatch,
                                                                         elsewhere):
    fake = secret_fake()
    if elsewhere:
        fake.found[("run", "jobs", "list")] = [run_item("pdt-report", "europe-west1", RUNNER)]
    assert google.destroy(destroy_app(project, monkeypatch, fake), True) == 0
    assert (fake.ran("secrets", "delete") == []) == elsewhere
    assert (fake.ran("storage", "rm") == []) == elsewhere
    assert fake.ran("secrets", "remove-iam-policy-binding") == []


def test_destroy_removes_the_secret_access_no_remaining_deploy_needs(project, monkeypatch,
                                                                     capsys):
    fake = secret_fake(account=OWN, other_jobs=("pdt-other",))
    fake.found[("run", "services", "list")] = [
        run_item("pdt-report", "europe-west1", "pdt-web-report@my-project.iam.gserviceaccount.com",
                 job=False)]
    assert google.destroy(destroy_app(project, monkeypatch, fake), True) == 0
    assert fake.ran("secrets", "delete") == []
    removed = fake.ran("secrets", "remove-iam-policy-binding")
    assert [(call[call.index("--member") + 1], call[call.index("--role") + 1])
            for call in removed] == [(f"serviceAccount:{OWN}", role) for role in SECRET_ROLES]
    assert fake.ran("iam", "service-accounts", "delete") == []
    out = " ".join(capsys.readouterr().out.split())
    assert f"removing {OWN} access to secret pdt-report-env" in out


class FakeStore:
    def usage(self):
        return (0, 0)


@pytest.mark.parametrize("other_account, revoked", [("pdt-b", True), ("pdt-a", False)])
def test_destroy_keeps_the_write_grant_only_for_an_account_still_in_use(project, monkeypatch,
                                                                         other_account, revoked):
    fake = destroy_fake()
    fake.found[("storage", "buckets", "describe")] = {"labels": {"managed-by": "pdt"}}
    fake.found[("run", "jobs", "list")] = [run_item(
        "pdt-report", "europe-west1", f"{other_account}@my-project.iam.gserviceaccount.com")]
    app = destroy_app(project, monkeypatch, fake)
    app["storage"] = True
    monkeypatch.setenv("PDT_CLOUD_RUN_SERVICE_ACCOUNT", "pdt-a@my-project.iam.gserviceaccount.com")
    monkeypatch.setattr(google, "deployer_store", lambda project, name: FakeStore())
    monkeypatch.setattr(google, "folder_grant_exists", lambda bucket, name, sa: True)
    monkeypatch.setattr(google, "bucket_grant_exists", lambda bucket, name, sa: False)
    monkeypatch.setattr(google, "warn_if_locked", lambda store, name: None)
    revokes = []
    monkeypatch.setattr(google, "revoke_store_access", lambda *args: revokes.append(args))
    assert google.destroy(app, True) == 0
    assert [args[2] for args in revokes] == (
        ["pdt-a@my-project.iam.gserviceaccount.com"] if revoked else [])


def test_an_incomplete_list_of_other_regions_keeps_every_shared_resource(project, monkeypatch,
                                                                         capsys):
    fake = secret_fake()
    app = destroy_app(project, monkeypatch, fake, unreachable=["europe-west1"])
    assert google.destroy(app, True) == 0
    assert fake.ran("secrets", "delete") == []
    assert fake.ran("secrets", "remove-iam-policy-binding") == []
    assert fake.ran("iam", "service-accounts", "delete") == []
    assert fake.ran("storage", "rm") == []
    out = " ".join(capsys.readouterr().out.split())
    assert ("The list of Cloud Run jobs and services in other regions is incomplete "
            "(no answer from europe-west1)") in out
    assert f"service account {BUILDER}" in out and f"bucket {BUILDS}" in out


def test_the_cloud_run_list_names_what_made_it_incomplete(monkeypatch):
    pages = [{"items": [{"n": 1}], "metadata": {"continue": "next"}, "unreachable": ["asia-east1"]},
             {"items": [{"n": 2}], "unreachable": ["europe-west1"]}]
    urls = []
    monkeypatch.setattr(google, "run_quiet", lambda *args: "token\n")
    monkeypatch.setattr(google, "fetch_json", lambda req, timeout: urls.append(req.full_url)
                        or pages[len(urls) - 1])
    assert google.cloud_run_list("my-project", "services") == (
        [{"n": 1}, {"n": 2}], ["asia-east1", "europe-west1"], "")
    assert urls == [f"{google.RUN_API}/serving.knative.dev/v1/namespaces/my-project/services",
                    f"{google.RUN_API}/serving.knative.dev/v1/namespaces/my-project/services"
                    "?continue=next"]

    def second_page_fails(req, timeout):
        if "continue" in req.full_url:
            raise OSError("HTTP Error 503")
        return pages[0]

    monkeypatch.setattr(google, "fetch_json", second_page_fails)
    assert google.cloud_run_list("my-project", "jobs") == (
        [{"n": 1}], ["asia-east1"], "the list of Cloud Run jobs failed: HTTP Error 503")


def test_the_remaining_deploys_name_every_region_and_error_once(monkeypatch):
    service = run_item("pdt-report", "us-central1", RUNNER, job=False)
    listed = {"jobs": ([run_item("pdt-report", "us-central1", RUNNER),
                        run_item("pdt-report", "europe-west1", RUNNER)],
                       ["asia-east1", "europe-north1"], ""),
              "services": ([service], ["asia-east1"],
                           "the list of Cloud Run services failed: HTTP 403")}
    monkeypatch.setattr(google, "cloud_run_list", lambda project, kind: listed[kind])
    jobs, services, missing = google.remaining_deploys("my-project", "jobs", "pdt-report",
                                                       "us-central1")
    assert [google.run_job_identity(item) for item in jobs] == [("pdt-report", "europe-west1")]
    assert services == [service]
    assert missing == ("no answer from asia-east1, europe-north1; "
                       "the list of Cloud Run services failed: HTTP 403")


def test_the_uses_include_the_folders_a_job_or_a_service_mounts():
    def volume(options):
        return {"name": "read", "csi": {"driver": "gcsfuse.run.googleapis.com",
                "volumeAttributes": {"mountOptions": options}}}

    job = run_item("pdt-worker", "us-central1", RUNNER)
    job["spec"]["template"]["spec"]["template"]["spec"]["volumes"] = [
        volume("only-dir=warehouse,uid=1000")]
    service = run_item("pdt-dashboard", "us-central1", "pdt-web-dashboard@x", job=False)
    service["spec"]["template"]["spec"]["volumes"] = [volume("uid=1000;only-dir=reports")]
    assert google.deploy_uses([job, service]) == (
        {RUNNER, "pdt-web-dashboard@x"},
        {(RUNNER, "worker"), (RUNNER, "warehouse"),
         ("pdt-web-dashboard@x", "dashboard"), ("pdt-web-dashboard@x", "reports")})


def test_destroy_removes_a_web_account_the_job_ran_as_when_no_deploy_needs_it(project,
                                                                              monkeypatch):
    web_account = "pdt-web-dashboard@my-project.iam.gserviceaccount.com"
    fake = destroy_fake()
    fake.found[("iam", "service-accounts", "list")].append(
        {"email": web_account, "displayName": "pdt web app dashboard"})
    app = destroy_app(project, monkeypatch, fake)
    monkeypatch.setenv("PDT_CLOUD_RUN_SERVICE_ACCOUNT", web_account)
    assert google.destroy(app, True) == 0
    assert [call[3] for call in fake.ran("iam", "service-accounts", "delete")] == [
        web_account, BUILDER]


@pytest.mark.parametrize("runner_in_use", [False, True])
def test_destroy_removes_the_runner_when_no_remaining_deploy_runs_as_it(project, monkeypatch,
                                                                          runner_in_use):
    fake = destroy_fake()
    fake.found[("iam", "service-accounts", "list")].append(
        {"email": RUNNER, "displayName": "pdt job runner"})
    custom = "pdt-a@my-project.iam.gserviceaccount.com"
    fake.found[("run", "jobs", "list")] = [
        run_item("pdt-other", "us-east4", RUNNER if runner_in_use else custom)]
    app = destroy_app(project, monkeypatch, fake)
    monkeypatch.setenv("PDT_CLOUD_RUN_SERVICE_ACCOUNT", custom)
    assert google.destroy(app, True) == 0
    deleted = [call[3] for call in fake.ran("iam", "service-accounts", "delete")]
    assert deleted == ([] if runner_in_use else [RUNNER])


def test_destroy_removes_the_write_grant_when_storage_is_now_off(project, monkeypatch):
    fake = destroy_fake()
    fake.found[("storage", "buckets", "describe")] = {"labels": {"managed-by": "pdt"}}
    app = destroy_app(project, monkeypatch, fake)
    assert app["storage"] is False
    monkeypatch.setattr(google, "folder_grant_exists", lambda bucket, name, sa: True)
    monkeypatch.setattr(google, "bucket_grant_exists", lambda bucket, name, sa: False)
    monkeypatch.setattr(google, "warn_if_locked", lambda store, name: pytest.fail("no store"))
    revokes = []
    monkeypatch.setattr(google, "revoke_store_access", lambda *args: revokes.append(args))
    assert google.destroy(app, True) == 0
    assert [args[1:3] for args in revokes] == [("report", OWN)]


def test_a_failed_read_of_the_secret_policy_keeps_the_job_access(project, monkeypatch, capsys):
    fake = secret_fake(account=OWN)
    fake.found[("iam", "service-accounts", "list")].append(
        {"email": OWN, "displayName": "pdt job runner report"})
    fake.found[("storage", "buckets", "describe")] = {"labels": {"managed-by": "pdt"}}
    fake.found[("run", "services", "list")] = [
        run_item("pdt-report", "europe-west1", "pdt-web-report@my-project.iam.gserviceaccount.com",
                 job=False)]
    app = destroy_app(project, monkeypatch, fake)
    monkeypatch.setattr(google, "secret_roles", lambda project, sid, sa: None)
    monkeypatch.setattr(google, "folder_grant_exists", lambda bucket, name, sa: True)
    monkeypatch.setattr(google, "revoke_store_access", lambda *args: pytest.fail("revoked"))
    assert google.destroy(app, True) == 0
    assert fake.ran("secrets", "remove-iam-policy-binding") == []
    assert [call[3] for call in fake.ran("iam", "service-accounts", "delete")] == []
    out = " ".join(capsys.readouterr().out.split())
    assert f"Reading who may use secret pdt-report-env failed; keeping {OWN} and its access" in out


@pytest.mark.parametrize("elsewhere", [False, True])
def test_destroy_deletes_the_jobs_account_unless_the_app_runs_in_another_region(
        project, monkeypatch, elsewhere):
    fake = destroy_fake(builder=False)
    fake.found[("iam", "service-accounts", "list")] = [
        {"email": OWN, "displayName": "pdt job runner report"}]
    if elsewhere:
        fake.found[("run", "jobs", "list")] = [run_item("pdt-report", "europe-west1", OWN)]
    assert google.destroy(destroy_app(project, monkeypatch, fake), True) == 0
    deleted = [call[3] for call in fake.ran("iam", "service-accounts", "delete")]
    assert deleted == ([] if elsewhere else [OWN])


def moving_fake(*jobs):
    return FakeGcloud(found={
        ("run", "jobs", "describe"): run_item("pdt-report", "us-central1", RUNNER),
        ("run", "jobs", "get-iam-policy"): {"bindings": [
            {"role": "roles/run.invoker", "members": [f"serviceAccount:{RUNNER}"]}]},
        ("run", "jobs", "list"): list(jobs),
        ("iam", "service-accounts", "list"): [{"email": RUNNER, "displayName": "pdt job runner"}],
    })


def moving_app(project, monkeypatch, fake, executions_error=""):
    """The REST lists answer from `fake` and land in its calls as ["rest", kind]."""
    app = deploy_app(project, monkeypatch, fake, cloud=True)

    def cloud_run_list(project, kind):
        fake.calls.append(["rest", kind])
        error = executions_error if kind == "executions" else ""
        return fake.found.get(("run", kind, "list"), []), [], error

    monkeypatch.setattr(google, "cloud_run_list", cloud_run_list)
    return app


def test_a_job_that_runs_as_pdt_runner_moves_to_its_own_account(project, monkeypatch):
    fake = moving_fake()
    assert google.deploy(moving_app(project, monkeypatch, fake), True) == 0
    assert fake.ran("iam", "service-accounts", "create", "pdt-job-report")
    [deploy] = fake.ran("run", "jobs", "deploy")
    assert deploy[deploy.index("--service-account") + 1] == OWN
    [schedule] = fake.ran("scheduler", "jobs", "create")
    assert schedule[schedule.index("--oauth-service-account-email") + 1] == OWN
    assert fake.ran("run", "jobs", "remove-iam-policy-binding", "pdt-report", "--region",
                    "us-central1", "--project", "my-project", "--member",
                    f"serviceAccount:{RUNNER}", "--role", "roles/run.invoker")
    assert ["iam", "service-accounts", "delete", RUNNER, "--project", "my-project",
            "--quiet"] in fake.calls


def test_pdt_runner_stays_while_another_job_runs_as_it(project, monkeypatch):
    fake = moving_fake(run_item("pdt-other", "us-east4", RUNNER))
    assert google.deploy(moving_app(project, monkeypatch, fake), True) == 0
    assert fake.ran("iam", "service-accounts", "delete") == []


def runner_grants(fake, monkeypatch):
    """pdt-runner holds the folder grant and the secret roles of report; the calls that remove
    the folder grant land in the returned list."""
    fake.found[("storage", "buckets", "describe")] = {"labels": {"managed-by": "pdt"}}
    fake.found[("secrets", "describe")] = {"labels": {"managed-by": "pdt"}}
    fake.found[("secrets", "get-iam-policy")] = {"bindings": [
        {"role": role, "members": [f"serviceAccount:{RUNNER}"]} for role in SECRET_ROLES]}
    monkeypatch.setattr(google, "folder_grant_exists", lambda bucket, name, sa: sa == RUNNER)
    revokes = []
    monkeypatch.setattr(google, "revoke_store_access", lambda *args: revokes.append(args[1:3]))
    return revokes


def test_the_move_keeps_what_the_same_app_in_another_region_uses(project, monkeypatch):
    fake = moving_fake(run_item("pdt-report", "europe-west1", RUNNER))
    app = moving_app(project, monkeypatch, fake)
    revokes = runner_grants(fake, monkeypatch)
    assert google.deploy(app, True) == 0
    assert revokes == []
    assert fake.ran("secrets", "remove-iam-policy-binding") == []
    assert fake.ran("run", "jobs", "remove-iam-policy-binding")
    assert fake.ran("iam", "service-accounts", "delete") == []


def test_a_deploy_after_a_stopped_move_finishes_the_clean_up(project, monkeypatch):
    fake = moving_fake()
    fake.found[("run", "jobs", "describe")] = run_item("pdt-report", "us-central1", OWN)
    fake.found[("iam", "service-accounts", "list")].append(
        {"email": OWN, "displayName": "pdt job runner report"})
    fake.found[("scheduler", "jobs", "describe")] = {"httpTarget": {
        "uri": "https://us-central1-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/"
               "my-project/jobs/pdt-report:run",
        "oauthToken": {"serviceAccountEmail": RUNNER}}}
    app = moving_app(project, monkeypatch, fake)
    revokes = runner_grants(fake, monkeypatch)
    assert google.deploy(app, True) == 0
    assert fake.ran("scheduler", "jobs", "update")
    assert revokes == [("report", RUNNER)]
    assert len(fake.ran("secrets", "remove-iam-policy-binding")) == 2
    assert fake.ran("run", "jobs", "remove-iam-policy-binding")
    assert ["iam", "service-accounts", "delete", RUNNER, "--project", "my-project",
            "--quiet"] in fake.calls


def assert_runner_kept(fake, revokes):
    assert revokes == []
    assert fake.ran("secrets", "remove-iam-policy-binding") == []
    assert fake.ran("run", "jobs", "remove-iam-policy-binding") == []
    assert fake.ran("iam", "service-accounts", "delete") == []


@pytest.mark.parametrize("job", ["pdt-report", "pdt-other"])
def test_a_run_in_progress_as_pdt_runner_keeps_it_until_the_next_deploy(project, monkeypatch,
                                                                       capsys, job):
    fake = moving_fake()
    fake.found[("run", "executions", "list")] = [
        {"metadata": {"name": f"{job}-abcde"},
         "spec": {"template": {"spec": {"serviceAccountName": RUNNER}}}, "status": {}},
        {"metadata": {"name": "pdt-report-fghij"},
         "spec": {"template": {"spec": {"serviceAccountName": RUNNER}}},
         "status": {"completionTime": "2026-11-16T10:00:00Z"}}]
    app = moving_app(project, monkeypatch, fake)
    revokes = runner_grants(fake, monkeypatch)
    assert google.deploy(app, True) == 0
    assert_runner_kept(fake, revokes)
    out = " ".join(capsys.readouterr().out.split())
    assert (f"A run as {RUNNER} has not ended; {RUNNER} keeps its access until the next deploy "
            "after the run ends") in out


def test_the_runs_are_read_after_the_job_and_its_schedule_change(project, monkeypatch):
    fake = moving_fake()
    app = moving_app(project, monkeypatch, fake)
    assert google.deploy(app, True) == 0
    order = [call[:3] for call in fake.calls]
    runs = order.index(["rest", "executions"])
    assert runs > order.index(["run", "jobs", "deploy"])
    assert runs > order.index(["scheduler", "jobs", "create"])
    assert runs < order.index(["run", "jobs", "remove-iam-policy-binding"])


def test_an_unreadable_list_of_runs_keeps_pdt_runner_and_its_access(project, monkeypatch,
                                                                    capsys):
    fake = moving_fake()
    app = moving_app(project, monkeypatch, fake,
                     executions_error="the list of Cloud Run executions failed: HTTP Error 403")
    revokes = runner_grants(fake, monkeypatch)
    assert google.deploy(app, True) == 0
    assert_runner_kept(fake, revokes)
    out = " ".join(capsys.readouterr().out.split())
    assert ("The list of Cloud Run job runs in the project is incomplete (the list of Cloud Run "
            f"executions failed: HTTP Error 403); keeping {RUNNER} and its access") in out


def test_an_unreadable_secret_policy_keeps_pdt_runner_and_its_access(project, monkeypatch,
                                                                     capsys):
    fake = moving_fake()
    app = moving_app(project, monkeypatch, fake)
    revokes = runner_grants(fake, monkeypatch)
    monkeypatch.setattr(google, "secret_roles", lambda project, sid, sa: None)
    assert google.deploy(app, True) == 0
    assert_runner_kept(fake, revokes)
    assert ["rest", "executions"] not in fake.calls
    out = " ".join(capsys.readouterr().out.split())
    assert f"Reading who may use secret pdt-report-env failed; keeping {RUNNER} and its access" in out


def test_the_plan_says_the_clean_up_waits_for_runs_as_pdt_runner(project, monkeypatch):
    fake = moving_fake()
    app = moving_app(project, monkeypatch, fake)
    runner_grants(fake, monkeypatch)
    shown = []
    monkeypatch.setattr(google, "show_plan", lambda actions, cost: shown.extend(plain(actions)))
    google.deploy(app, True, plan_only=True)
    assert ["rest", "executions"] not in fake.calls
    assert (f"remove {RUNNER} write access to {google.store_bucket('my-project')}/report/ "
            f"(after the update, if no run as {RUNNER} is in progress)") in shown


@pytest.mark.parametrize("created", [True, False])
def test_the_schedule_waits_for_an_account_this_deploy_created(project, monkeypatch, created):
    fake = moving_fake()
    if not created:
        fake.found[("iam", "service-accounts", "list")].append(
            {"email": OWN, "displayName": "pdt job runner report"})
    app = moving_app(project, monkeypatch, fake)
    sleeps = []
    monkeypatch.setattr(google.time, "sleep", sleeps.append)
    answers = [subprocess.CompletedProcess([], 1, "", "ERROR: (gcloud.scheduler.jobs.create.http) "
                                           "NOT_FOUND: Requested entity was not found.")]

    def scheduler_not_ready(command, *args, **kwargs):
        if command[1:4] == ["scheduler", "jobs", "create"] and answers:
            fake.calls.append(list(command[1:]))
            return answers.pop()
        return fake(command, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", scheduler_not_ready)
    if created:
        assert google.deploy(app, True) == 0
        assert len(fake.ran("scheduler", "jobs", "create")) == 2
        assert sleeps == [google.RETRY_WAITS[0]]
    else:
        with pytest.raises(SystemExit):
            google.deploy(app, True)
        assert len(fake.ran("scheduler", "jobs", "create")) == 1
        assert sleeps == []


def test_an_incomplete_list_of_deploys_keeps_pdt_runner_and_its_access(project, monkeypatch,
                                                                       capsys):
    fake = moving_fake()
    app = moving_app(project, monkeypatch, fake)
    monkeypatch.setattr(google, "cloud_run_list", lambda project, kind: ([], ["europe-west1"], ""))
    revokes = runner_grants(fake, monkeypatch)
    assert google.deploy(app, True) == 0
    assert revokes == []
    assert fake.ran("run", "jobs", "remove-iam-policy-binding") == []
    assert fake.ran("iam", "service-accounts", "delete") == []
    out = " ".join(capsys.readouterr().out.split())
    assert f"incomplete (no answer from europe-west1); keeping {RUNNER} and its access" in out


def test_a_refused_act_as_names_the_role_and_the_account(project, monkeypatch, capsys):
    fake = moving_fake()
    app = moving_app(project, monkeypatch, fake)

    def refuse(command, *args, **kwargs):
        if command[1:4] == ["run", "jobs", "deploy"]:
            return subprocess.CompletedProcess(command, 1, "", (
                "ERROR: (gcloud.run.jobs.deploy) PERMISSION_DENIED: Permission "
                f"'iam.serviceaccounts.actAs' denied on service account {OWN}"))
        return fake(command, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", refuse)
    with pytest.raises(SystemExit):
        google.deploy(app, True)
    out = " ".join(capsys.readouterr().out.split())
    assert (f"Cloud Run refused to run pdt-report as {OWN} for your Google account; add the "
            f"role roles/iam.serviceAccountUser for your account on service account {OWN}") in out


def test_destroy_without_a_cloud_build_ever_tolerates_their_absence(project, monkeypatch):
    fake = destroy_fake(builder=False, bucket=False)
    assert google.destroy(destroy_app(project, monkeypatch, fake), True) == 0
    assert fake.ran("iam", "service-accounts", "delete") == []
    assert fake.ran("storage", "rm") == []


def test_destroy_keeps_a_bucket_pdt_does_not_manage(project, monkeypatch):
    fake = destroy_fake()
    fake.found[("storage", "buckets", "describe", f"gs://{BUILDS}")] = {"labels": {}}
    google.destroy(destroy_app(project, monkeypatch, fake), True)
    assert fake.ran("storage", "rm") == []
