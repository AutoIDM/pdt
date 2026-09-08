import copy
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from pdt import deploy_google_cloud, terraform_google


def test_configuration_pins_the_google_provider():
    config = terraform_google.configuration(
        {"project": "example", "region": "us-central1"}, {"google_project_service": {}})

    assert config["terraform"]["required_providers"]["google"] == {
        "source": "hashicorp/google", "version": "= 8.1.0"}
    assert config["provider"]["google"]["project"] == "example"


def test_global_resources_enable_apis_and_create_the_runner():
    resources = terraform_google.global_resources(
        "example", "pdt-runner@example.iam.gserviceaccount.com")

    assert resources["google_project_service"]["pdt"]["disable_on_destroy"] is False
    assert resources["google_service_account"]["runner"]["account_id"] == "pdt-runner"


def test_app_resources_do_not_include_secret_values():
    resources = terraform_google.app_resources(
        "report", "example", "us-central1", "0 8 * * *", "UTC",
        "us-central1-docker.pkg.dev/example/pdt/report@sha256:abc",
        "pdt-runner@example.iam.gserviceaccount.com", "pdt-report-env", True, True)
    rendered = repr(resources)

    assert "secret_data" not in rendered
    assert resources["google_cloud_run_v2_job"]["job"]["template"]["template"]["containers"][0]["image"].endswith("@sha256:abc")
    members = resources["google_secret_manager_secret_iam_member"]
    assert {item["role"] for item in members.values()} == {
        "roles/secretmanager.secretAccessor", "roles/secretmanager.secretVersionAdder"}


def test_import_addresses_match_the_generated_resource_names():
    assert terraform_google.address("cloud_run_v2_job", "job") == "google_cloud_run_v2_job.job"
    assert terraform_google.address("artifact_registry_repository", "images") == (
        "google_artifact_registry_repository.images")


def test_custom_service_account_is_not_a_managed_resource():
    resources = terraform_google.global_resources("example", "custom@example.iam.gserviceaccount.com")
    assert "google_service_account" not in resources


def test_app_dependencies_create_secret_permissions_before_job_and_job_before_schedule():
    resources = terraform_google.app_resources(
        "report", "example", "us-central1", "0 8 * * *", "UTC", "registry/image@sha256:abc",
        "pdt-runner@example.iam.gserviceaccount.com", "pdt-report-env", True, True)
    job = resources["google_cloud_run_v2_job"]["job"]
    assert job["deletion_protection"] is False
    assert job["depends_on"] == ["google_secret_manager_secret_iam_member.runner_0",
                                  "google_secret_manager_secret_iam_member.runner_1"]
    assert resources["google_cloud_run_v2_job_iam_member"]["invoker"]["name"] == (
        "${google_cloud_run_v2_job.job.name}")
    assert resources["google_cloud_scheduler_job"]["schedule"]["depends_on"] == [
        "google_cloud_run_v2_job_iam_member.invoker"]
    assert resources["google_secret_manager_secret_iam_member"]["runner_0"]["secret_id"] == (
        "${google_secret_manager_secret.env.secret_id}")


def test_discovery_skips_disabled_apis(monkeypatch):
    monkeypatch.setattr(deploy_google_cloud, "run_quiet", lambda *_args: "")
    read = Mock(side_effect=AssertionError("disabled API must not be queried"))
    monkeypatch.setattr(deploy_google_cloud, "read_json_or_none", read)
    assert deploy_google_cloud.imports_for_deploy(
        "report", "example", "us-central1", "pdt", "pdt-runner@example.iam.gserviceaccount.com", True, False) == ({}, {}, {})
    read.assert_not_called()


def test_api_import_uses_provider_project_service_format(monkeypatch):
    monkeypatch.setattr(deploy_google_cloud, "run_quiet", lambda *_args: "iam.googleapis.com")
    monkeypatch.setattr(deploy_google_cloud, "read_json_or_none", lambda *_args: {
        "displayName": "pdt job runner", "description": "Managed by PDT"})
    global_imports, _, _ = deploy_google_cloud.imports_for_deploy(
        "report", "example", "us-central1", "pdt", "pdt-runner@example.iam.gserviceaccount.com", False, False)
    assert global_imports['google_project_service.pdt["iam.googleapis.com"]'] == "example/iam.googleapis.com"


def test_custom_service_account_is_read_without_import(monkeypatch):
    monkeypatch.setattr(deploy_google_cloud, "run_quiet", lambda *_args: "iam.googleapis.com")
    monkeypatch.setattr(deploy_google_cloud, "read_json_or_none", lambda *_args: {"displayName": "Customer identity"})
    global_imports, _, _ = deploy_google_cloud.imports_for_deploy(
        "report", "example", "us-central1", "pdt", "custom@example.iam.gserviceaccount.com", False, False)
    assert "google_service_account.runner" not in global_imports


@pytest.fixture
def app(tmp_path):
    return {"name": "report", "dir": tmp_path, "schedule": "0 8 * * *", "timezone": "UTC",
            "platform": {"project": "example", "region": "us-central1"},
            "storage": False}


@pytest.fixture
def deployment(monkeypatch):
    events = []
    workspaces = {}

    class Workspace:
        def __init__(self, name, document):
            self.name = name
            self.configuration = copy.deepcopy(document)

        def plan(self, destroy=False):
            return SimpleNamespace(destroy=destroy, actions=[])

        def apply(self, plan):
            events.append((self.name, "destroy" if plan.destroy else "apply", copy.deepcopy(self.configuration)))

    class Deployment:
        def __enter__(self):
            events.append(("backend",))
            return self

        def __exit__(self, *_args):
            pass

        def save(self):
            events.append(("save",))

        def workspace(self, name, document, imports=None, retain=False):
            if name in workspaces and retain:
                previous = copy.deepcopy(workspaces[name].configuration)
                for kind, resources in document["resource"].items():
                    previous["resource"].setdefault(kind, {}).update(resources)
                document = previous
            workspaces[name] = Workspace(name, document)
            return workspaces[name]

        def existing_workspace(self, name):
            return workspaces.get(name)

        def finish_destroy(self):
            events.append(("finish",))

    instance = Deployment()
    monkeypatch.setattr(deploy_google_cloud, "deployment_context", lambda *_args: instance)
    monkeypatch.setattr(deploy_google_cloud, "preflight", lambda _app, project, _yes: project)
    monkeypatch.setattr(deploy_google_cloud, "confirm", lambda *_args: events.append(("confirm",)) or True)
    return instance, events, workspaces


def test_deploy_writes_first_secret_version_before_creating_job(app, deployment, monkeypatch):
    instance, events, _ = deployment
    instance.workspace("global", {"resource": {"google_project_service": {"billing": {"service": "cloudbilling.googleapis.com"}}}})
    monkeypatch.setattr(deploy_google_cloud, "imports_for_deploy", lambda *_args: ({}, {}, {}))
    monkeypatch.setattr(deploy_google_cloud, "cost_estimate", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(deploy_google_cloud, "gather_secrets", lambda *_args: {"TOKEN": "private-value"})
    monkeypatch.setattr(deploy_google_cloud, "build_image", lambda *_args: events.append(("build",)))

    def run(*args, **kwargs):
        if args[:3] == ("secrets", "versions", "add"):
            assert json.loads(kwargs["data"]) == {"TOKEN": "private-value"}
            events.append(("secret-value",))
            return ""
        return "sha256:abcd"

    monkeypatch.setattr(deploy_google_cloud, "run_quiet", run)
    assert deploy_google_cloud.deploy(app, True) == 0
    secret_index = events.index(("secret-value",))
    app_applies = [(index, item[2]) for index, item in enumerate(events) if item[:2] == ("app", "apply")]
    assert app_applies[0][0] < secret_index < app_applies[1][0]
    assert "google_cloud_run_v2_job" not in app_applies[0][1]["resource"]
    assert "google_secret_manager_secret_iam_member" in app_applies[0][1]["resource"]
    assert "google_cloud_run_v2_job" in app_applies[1][1]["resource"]
    for event in events:
        if len(event) == 3:
            assert "private-value" not in json.dumps(event[2])
        if event[:2] == ("global", "apply"):
            assert "billing" in event[2]["resource"]["google_project_service"]


@pytest.mark.parametrize("other_app", [True, False])
def test_destroy_adopts_legacy_resources_and_removes_only_app_images(app, deployment, monkeypatch, other_app):
    _, events, _ = deployment
    default_account = "pdt-runner@example.iam.gserviceaccount.com"
    image = "us-central1-docker.pkg.dev/example/pdt/report@sha256:old"
    current = {"metadata": {"labels": {"managed-by": "pdt"}}, "spec": {"template": {"spec": {"template": {"spec": {
        "serviceAccountName": default_account, "containers": [{"image": image}],
    }}}}}}
    monkeypatch.setattr(deploy_google_cloud, "gather_secrets", Mock(side_effect=AssertionError("destroy must not read env values")))
    monkeypatch.setattr(deploy_google_cloud, "read_json_or_none", lambda *args: current if args[0] == "run" else {"labels": {"managed-by": "pdt"}})
    global_imports = {"google_service_account.runner": "projects/example/serviceAccounts/" + default_account}
    shared_imports = {"google_artifact_registry_repository.images": "projects/example/locations/us-central1/repositories/pdt"}
    app_imports = {
        "google_cloud_run_v2_job.job": "projects/example/locations/us-central1/jobs/pdt-report",
        "google_cloud_scheduler_job.schedule": "projects/example/locations/us-central1/jobs/pdt-report",
        "google_secret_manager_secret.env": "projects/example/secrets/pdt-report-env",
    }

    def imports(*args, **kwargs):
        assert args[5] is True
        return global_imports, shared_imports, app_imports

    monkeypatch.setattr(deploy_google_cloud, "imports_for_deploy", imports)

    def run(*args, **_kwargs):
        if args[:2] == ("services", "list"):
            return "run.googleapis.com\nsecretmanager.googleapis.com\nartifactregistry.googleapis.com"
        events.append(("command", args))
        return ""

    def listing(*args):
        if args[0] == "run":
            if other_app:
                return [{"name": "projects/example/locations/us-central1/jobs/pdt-other"}]
            return []
        images = [{"package": "us-central1-docker.pkg.dev/example/pdt/report"}]
        if other_app:
            images.append({"package": "us-central1-docker.pkg.dev/example/pdt/other"})
        return images

    monkeypatch.setattr(deploy_google_cloud, "run_quiet", run)
    monkeypatch.setattr(deploy_google_cloud, "list_json", listing)
    assert deploy_google_cloud.destroy(app, True) == 0
    assert events.index(("confirm",)) < events.index(("backend",))
    app_apply = next(item[2] for item in events if item[:2] == ("app", "apply"))
    assert app_apply["resource"]["google_cloud_run_v2_job"]["job"]["template"]["template"]["containers"][0]["image"] == image
    assert app_apply["resource"]["google_cloud_run_v2_job"]["job"]["deletion_protection"] is False
    assert "google_secret_manager_secret" in app_apply["resource"]
    command = next(item[1] for item in events if item[0] == "command")
    assert command[:5] == ("artifacts", "docker", "images", "delete", "us-central1-docker.pkg.dev/example/pdt/report")
    assert any(item[:2] == ("shared", "destroy") for item in events) is not other_app
    assert any(item[:2] == ("global", "destroy") for item in events) is not other_app
    assert events[-1] == ("finish",)


def test_declined_destroy_does_not_create_backend(app, deployment, monkeypatch):
    _, events, _ = deployment
    monkeypatch.setattr(deploy_google_cloud, "run_quiet", lambda *_args: "")
    monkeypatch.setattr(deploy_google_cloud, "imports_for_deploy", lambda *_args, **_kwargs: ({}, {}, {}))
    monkeypatch.setattr(deploy_google_cloud, "confirm", lambda *_args: False)
    assert deploy_google_cloud.destroy(app, False) == 1
    assert ("backend",) not in events
