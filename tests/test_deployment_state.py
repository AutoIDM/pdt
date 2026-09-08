import json

import pytest

from pdt import config, deploy, terraform


class Workspace:
    def __init__(self, directory, configuration, *_args, **_kwargs):
        self.directory = directory
        self.configuration = configuration
        self.persist = None

    def init(self):
        self.persist(self.configuration)

    def import_resources(self, _imports):
        pass


class Store:
    def __init__(self):
        self.values = {}

    def prepare(self):
        pass

    def acquire(self):
        pass

    def check_teardown(self):
        pass

    def finalize_prepare(self):
        pass

    def release(self):
        pass

    def backend(self, _key):
        return {}

    def read(self, key):
        return self.values.get(key)

    def write(self, key, value):
        self.values[key] = value

    def delete(self, key):
        self.values.pop(key, None)

    def cleanup(self):
        pass


def test_retained_workspace_merges_saved_shared_resources(project, monkeypatch):
    monkeypatch.setattr(terraform, "TerraformWorkspace", Workspace)
    app = {"name": "report"}
    deployment = terraform.Deployment(app, "windows", {"machine": "host"}, assume_yes=True)
    existing = {"resource": {"terraform_data": {"first": {"input": "one"}}}}
    deployment.write_configuration("shared", existing)
    assert deployment.read_configuration("shared") == existing

    with deployment:
        workspace = deployment.workspace(
            "shared", {"resource": {"terraform_data": {"second": {"input": "two"}}}},
            retain=True)

    assert workspace.configuration["resource"]["terraform_data"] == {
        "first": {"input": "one"}, "second": {"input": "two"}}


def test_cloud_owner_record_rejects_a_different_deployment_scope(project, monkeypatch):
    import pdt.terraform_store as stores

    store = Store()
    monkeypatch.setattr(stores, "cloud_store", lambda *_args: store)
    app = {"name": "report"}
    first = terraform.Deployment(app, "google-cloud", {"project": "one", "region": "us"}, assume_yes=True)
    with first:
        first.save()
    owner = store.values["owners/report.json"]

    second = terraform.Deployment(app, "google-cloud", {"project": "two", "region": "us"}, assume_yes=True)
    with pytest.raises(terraform.TerraformError, match="another location"):
        with second:
            second.save()

    assert store.values["owners/report.json"] == owner


def test_destroy_keeps_remote_state_when_the_owner_scope_differs(project, monkeypatch):
    import pdt.terraform_store as stores

    store = Store()
    monkeypatch.setattr(stores, "cloud_store", lambda *_args: store)
    app = {"name": "report"}
    deployment = terraform.Deployment(app, "google-cloud", {"project": "one", "region": "us"}, assume_yes=True)
    owner = {"provider": "google-cloud", "identity": {"project": "two", "region": "us"}}
    store.values["owners/report.json"] = json.dumps(owner).encode()
    store.values[deployment.key("app") + "/deployment.json"] = b"{}"

    with pytest.raises(terraform.TerraformError, match="another location"):
        with deployment:
            deployment.finish_destroy()

    assert store.values["owners/report.json"] == json.dumps(owner).encode()
    assert deployment.key("app") + "/deployment.json" in store.values


def test_destroy_loads_the_recorded_provider_and_identity(project, monkeypatch):
    app_dir = project / "report"
    app_dir.mkdir()
    (app_dir / "run.py").write_text("def main():\n    return 0\n")
    (project / "pdt.yml").write_text("platform:\n  provider: aws\n  region: us-east-1\n")
    record = project / ".pdt" / "terraform" / "deployments"
    record.mkdir(parents=True)
    (record / "report.json").write_text(json.dumps({
        "provider": "google-cloud", "identity": {"project": "saved", "region": "europe-west1"}}))

    app, provider = deploy._load("report", deployed=True)

    assert provider == "google-cloud"
    assert app["platform"]["project"] == "saved"
    assert app["platform"]["region"] == "europe-west1"


def test_saved_schedule_can_change_without_changing_deployment_identity(project):
    app = {"name": "report", "schedule": "0 1 * * *", "timezone": "local"}
    with terraform.Deployment(app, "windows", {"machine": "host"}) as deployment:
        deployment.save()
    app["schedule"] = "0 2 * * *"

    with terraform.Deployment(app, "windows", {"machine": "host"}) as deployment:
        deployment.save()

    record = json.loads((deployment.root / "deployments/report.json").read_text())
    assert record["schedule"] == "0 2 * * *"


def test_shared_location_change_cannot_replace_other_apps_resources(project):
    deployment = terraform.Deployment({"name": "report"}, "azure", {"resource_group": "pdt"})
    deployment.write_configuration("shared", {"resource": {"azurerm_key_vault": {"pdt": {"location": "eastus"}}}})

    with pytest.raises(terraform.TerraformError, match="platform.region"):
        deployment.workspace("shared", {"resource": {"azurerm_key_vault": {"pdt": {"location": "westus"}}}}, retain=True)


def test_destroy_context_uses_saved_schedule_after_config_changes(project, monkeypatch):
    app_dir = project / "report"
    app_dir.mkdir()
    (app_dir / "run.py").write_text("")
    (app_dir / "config.yml").write_text("schedule: broken\ntimezone: changed\n")
    monkeypatch.setenv("PDT_DEPLOYMENT_APP", "report")
    monkeypatch.setenv("PDT_DEPLOYMENT_CONTEXT", json.dumps({"schedule": "0 1 * * *", "timezone": "UTC"}))

    app = config.merged_app("report")

    assert app["schedule"] == "0 1 * * *"
    assert app["timezone"] == "UTC"


def test_local_lock_releases_after_an_exception(project):
    app = {"name": "report"}
    with pytest.raises(RuntimeError):
        with terraform.Deployment(app, "windows", {"machine": "host"}):
            with pytest.raises(terraform.TerraformError, match="local deployment lock"):
                with terraform.Deployment(app, "windows", {"machine": "host"}):
                    pass
            raise RuntimeError("interrupted")

    with terraform.Deployment(app, "windows", {"machine": "host"}):
        pass
