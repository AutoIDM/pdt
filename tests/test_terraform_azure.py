import pytest

from pdt import deploy_azure_container_apps, terraform_azure


def settings():
    return {
        "subscription": "11111111-1111-1111-1111-111111111111",
        "tenant_id": "22222222-2222-2222-2222-222222222222",
        "region": "eastus",
        "resource_group": "pdt",
        "vault": "pdt-vault",
        "workspace": "pdt-logs",
        "storage": "pdtstorage",
        "registry": "pdtregistry",
        "environment": "pdt",
        "identity": "pdt-runner",
        "deployer_object_id": "33333333-3333-3333-3333-333333333333",
        "deployer_principal_type": "User",
        "suffix": "abcdef1234",
    }


def test_functions_configuration_has_native_resources_without_secret_value():
    document = terraform_azure.function_configuration(
        {"name": "report", "function_app": "pdt-report", "cron": "0 9 * * *"},
        settings(), "https://pdt-vault.vault.azure.net/secrets/pdt-report-env/version")

    resources = document["resource"]
    assert document["terraform"]["required_providers"]["azurerm"]["version"] == "= 5.4.0"
    assert "azurerm_function_app_flex_consumption" in resources
    assert "azurerm_application_insights" in resources
    assert "azurerm_service_plan" in resources
    value = resources["azurerm_function_app_flex_consumption"]["function"]["app_settings"]["PDT_ENV_JSON"]
    assert "pdt-report-env" in value
    assert "secret-value" not in str(document)


def test_container_apps_configuration_has_scheduled_job_and_key_vault_reference():
    document = terraform_azure.container_app_configuration(
        {"name": "report", "job": "pdt-report", "cron": "0 9 * * *",
         "image": "pdtregistry.azurecr.io/report:latest"}, settings(),
        "https://pdt-vault.vault.azure.net/secrets/pdt-report-env/version")

    job = document["resource"]["azurerm_container_app_job"]["job"]
    assert job["schedule_trigger_config"][0]["cron_expression"] == "0 9 * * *"
    assert job["secret"][0]["key_vault_secret_id"].endswith("/version")
    assert "secret-value" not in str(document)


def test_shared_configurations_keep_each_runtime_resources_separate():
    functions = terraform_azure.shared_configuration(settings(), "functions")["resource"]
    containers = terraform_azure.shared_configuration(settings(), "container_apps")["resource"]

    assert "azurerm_storage_account" in functions
    assert "azurerm_container_registry" not in functions
    assert "azurerm_container_registry" in containers
    assert "azurerm_container_app_environment" in containers
    roles = containers["azurerm_role_assignment"]
    assert any(name.startswith("deployer_key_vault_officer_") for name in roles)


def test_import_ids_are_native_azure_resource_ids():
    values = terraform_azure.shared_imports(settings(), "container_apps")

    assert values["azurerm_resource_group.pdt"].endswith("/resourceGroups/pdt")
    assert any(value.endswith("/providers/Microsoft.ContainerRegistry/registries/pdtregistry")
               for value in values.values())
    app = {"name": "report", "function_app": "pdt-report"}
    assert terraform_azure.function_imports(app, settings())["azurerm_function_app_flex_consumption.function"].endswith("/sites/pdt-report")


def test_a_declined_plan_does_not_open_terraform_backend(monkeypatch):
    app = {"name": "report", "schedule": "0 9 * * *", "platform": {}, "dir": ".",
           "storage": False}
    values = settings()
    monkeypatch.setattr(deploy_azure_container_apps, "azure_settings", lambda _: values)
    monkeypatch.setattr(deploy_azure_container_apps, "preflight", lambda *_: values)
    monkeypatch.setattr(deploy_azure_container_apps, "az_json", lambda *_: None)
    monkeypatch.setattr(deploy_azure_container_apps, "check_shared_names", lambda *_: None)
    monkeypatch.setattr(deploy_azure_container_apps, "secret_state", lambda *_: (False, None))
    monkeypatch.setattr(deploy_azure_container_apps, "gather_secrets", lambda *_: {})
    monkeypatch.setattr(deploy_azure_container_apps, "cost_estimate_for", lambda *_: None)
    monkeypatch.setattr(deploy_azure_container_apps, "confirm", lambda *_: False)

    class Deployment:
        def __init__(self, *_):
            raise AssertionError("Terraform backend opened after a declined plan")

    monkeypatch.setattr(deploy_azure_container_apps, "Deployment", Deployment)
    assert deploy_azure_container_apps.deploy(app, False) == 1


def test_container_image_reference_uses_immutable_digest(monkeypatch):
    monkeypatch.setattr(deploy_azure_container_apps, "az_json", lambda *_: [
        {"digest": "sha256:abc", "tags": ["latest"]},
    ])

    assert deploy_azure_container_apps.image_reference("pdtregistry", "report") == (
        "pdtregistry.azurecr.io/report@sha256:abc")


def test_a_deploy_saves_identity_before_the_first_terraform_apply(monkeypatch):
    app = {"name": "report", "schedule": "0 9 * * *", "platform": {}, "dir": ".",
           "storage": False}
    values = settings()
    events = []
    monkeypatch.setattr(deploy_azure_container_apps, "azure_settings", lambda _: values)
    monkeypatch.setattr(deploy_azure_container_apps, "preflight", lambda *_: values)
    monkeypatch.setattr(deploy_azure_container_apps, "az_json", lambda *_: None)
    monkeypatch.setattr(deploy_azure_container_apps, "check_shared_names", lambda *_: None)
    monkeypatch.setattr(deploy_azure_container_apps, "secret_state", lambda *_: (False, None))
    monkeypatch.setattr(deploy_azure_container_apps, "gather_secrets", lambda *_: {})
    monkeypatch.setattr(deploy_azure_container_apps, "cost_estimate_for", lambda *_: None)
    monkeypatch.setattr(deploy_azure_container_apps, "confirm", lambda *_: True)
    monkeypatch.setattr(deploy_azure_container_apps, "terraform_shared_imports", lambda *_: {})

    class Workspace:
        def plan(self):
            return self

        def apply(self, _):
            events.append("apply")
            raise RuntimeError("first apply failed")

    class Deployment:
        def __init__(self, *_):
            pass

        def __enter__(self):
            events.append("backend")
            return self

        def __exit__(self, *_):
            pass

        def save(self):
            events.append("save")

        def workspace(self, *_args, **_kwargs):
            return Workspace()

    monkeypatch.setattr(deploy_azure_container_apps, "Deployment", Deployment)
    with pytest.raises(RuntimeError, match="first apply failed"):
        deploy_azure_container_apps.deploy(app, True)
    assert events == ["backend", "save", "apply"]


def test_legacy_shared_cleanup_imports_resources_without_saved_workspace(monkeypatch):
    values = settings()
    imports = {"azurerm_resource_group.pdt": "group"}
    monkeypatch.setattr(deploy_azure_container_apps, "terraform_shared_imports", lambda *_: imports)
    document = terraform_azure.shared_configuration(values, "container_apps")

    assert document["resource"]["azurerm_resource_group"]["pdt"]["name"] == "pdt"
    assert imports["azurerm_resource_group.pdt"] == "group"
