import json
import re
import subprocess
from pathlib import Path

import pytest

from conftest import add_app
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


def destroy_app(project, monkeypatch, fake):
    app = deploy_app(project, monkeypatch, fake, cloud=False)
    monkeypatch.setattr(google, "confirm", lambda actions, assume_yes, cost=None: True)
    return app


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
