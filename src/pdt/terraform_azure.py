"""Terraform configuration for Azure resources owned by PDT."""

from __future__ import annotations

import hashlib

from pdt.terraform import configuration


TAGS = {"managed-by": "pdt"}


def deployer_role_name(settings: dict[str, str]) -> str:
    return "deployer_key_vault_officer_" + hashlib.sha256(
        settings["deployer_object_id"].encode()).hexdigest()[:12]


def resource_id(settings: dict[str, str], provider: str, kind: str, name: str) -> str:
    return (f"/subscriptions/{settings['subscription']}/resourceGroups/"
            f"{settings['resource_group']}/providers/{provider}/{kind}/{name}")


def provider_settings(settings: dict[str, str]) -> dict:
    values = {
        "subscription_id": settings["subscription"],
        "resource_provider_registrations": "none",
        "resource_providers_to_register": [
            "Microsoft.App", "Microsoft.ContainerRegistry", "Microsoft.Insights",
            "Microsoft.KeyVault", "Microsoft.ManagedIdentity",
            "Microsoft.OperationalInsights", "Microsoft.Storage", "Microsoft.Web",
        ],
        "features": {"key_vault": {"purge_soft_delete_on_destroy": True}},
    }
    if settings.get("tenant_id"):
        values["tenant_id"] = settings["tenant_id"]
    return values


def _resource(type_name: str, name: str, values: dict) -> dict:
    return {type_name: {name: values}}


def _merge(*groups: dict) -> dict:
    merged: dict = {}
    for group in groups:
        for type_name, entries in group.items():
            merged.setdefault(type_name, {}).update(entries)
    return merged


def shared_configuration(settings: dict[str, str], runtime: str) -> dict:
    rg = settings["resource_group"]
    resources = _merge(
        _resource("azurerm_resource_group", "pdt", {
            "name": rg, "location": settings["region"], "tags": TAGS,
        }),
        _resource("azurerm_key_vault", "pdt", {
            "name": settings["vault"], "location": settings["region"],
            "resource_group_name": "${azurerm_resource_group.pdt.name}", "tenant_id": settings["tenant_id"],
            "sku_name": "standard", "rbac_authorization_enabled": True,
            "purge_protection_enabled": False, "soft_delete_retention_days": 7,
            "tags": TAGS,
        }),
        _resource("azurerm_log_analytics_workspace", "pdt", {
            "name": settings["workspace"], "location": settings["region"],
            "resource_group_name": "${azurerm_resource_group.pdt.name}", "sku": "PerGB2018", "retention_in_days": 30,
            "tags": TAGS,
        }),
        _resource("azurerm_role_assignment", deployer_role_name(settings), {
            "scope": "${azurerm_key_vault.pdt.id}",
            "role_definition_name": "Key Vault Secrets Officer",
            "principal_id": settings["deployer_object_id"],
            "principal_type": settings["deployer_principal_type"],
        }),
    )
    if runtime == "functions":
        resources = _merge(resources,
            _resource("azurerm_storage_account", "pdt", {
                "name": settings["storage"], "resource_group_name": "${azurerm_resource_group.pdt.name}",
                "location": settings["region"], "account_tier": "Standard",
                "account_replication_type": "LRS", "allow_nested_items_to_be_public": False,
                "tags": TAGS,
            }),
            _resource("azurerm_storage_container", "function_packages", {
                "name": "function-packages", "storage_account_id": "${azurerm_storage_account.pdt.id}",
                "container_access_type": "private",
            }),
        )
    if runtime == "container_apps":
        resources = _merge(resources,
            _resource("azurerm_container_registry", "pdt", {
                "name": settings["registry"], "resource_group_name": "${azurerm_resource_group.pdt.name}",
                "location": settings["region"], "sku": "Basic", "admin_enabled": False,
                "anonymous_pull_enabled": False, "data_endpoint_enabled": False,
                "azuread_authentication_as_arm_policy_enabled": True, "tags": TAGS,
            }),
            _resource("azurerm_user_assigned_identity", "runner", {
                "name": settings["identity"], "resource_group_name": "${azurerm_resource_group.pdt.name}",
                "location": settings["region"], "tags": TAGS,
            }),
            _resource("azurerm_container_app_environment", "pdt", {
                "name": settings["environment"], "resource_group_name": "${azurerm_resource_group.pdt.name}",
                "location": settings["region"],
                "log_analytics_workspace_id": "${azurerm_log_analytics_workspace.pdt.id}",
                "tags": TAGS,
            }),
            _resource("azurerm_role_assignment", "runner_acr_pull", {
                "scope": "${azurerm_container_registry.pdt.id}",
                "role_definition_name": "AcrPull",
                "principal_id": "${azurerm_user_assigned_identity.runner.principal_id}",
                "principal_type": "ServicePrincipal",
            }),
            _resource("azurerm_role_assignment", "runner_key_vault_user", {
                "scope": "${azurerm_key_vault.pdt.id}",
                "role_definition_name": "Key Vault Secrets User",
                "principal_id": "${azurerm_user_assigned_identity.runner.principal_id}",
                "principal_type": "ServicePrincipal",
            }),
        )
    return configuration("azurerm", provider_settings(settings), resources)


def function_configuration(app: dict, settings: dict[str, str], secret_uri: str | None) -> dict:
    name = app["function_app"]
    app_settings = {"PDT_SCHEDULE": f"0 {app['cron']}", "LOG_FORMAT": "json"}
    if secret_uri:
        app_settings["PDT_ENV_JSON"] = f"@Microsoft.KeyVault(SecretUri={secret_uri})"
    resources = _merge(
        _resource("azurerm_service_plan", "function", {
            "name": app.get("plan_name", f"pdt-{app['name']}-plan"), "resource_group_name": settings["resource_group"],
            "location": settings["region"], "os_type": "Linux", "sku_name": "FC1", "tags": {**TAGS, "pdt-app": app["name"]},
        }),
        _resource("azurerm_application_insights", "function", {
            "name": app.get("insights_name", name), "resource_group_name": settings["resource_group"],
            "location": settings["region"], "application_type": "web",
            "workspace_id": resource_id(settings, "Microsoft.OperationalInsights", "workspaces", settings["workspace"]),
            "tags": {**TAGS, "pdt-app": app["name"]},
        }),
        _resource("azurerm_function_app_flex_consumption", "function", {
            "name": name, "resource_group_name": settings["resource_group"],
            "location": settings["region"], "service_plan_id": "${azurerm_service_plan.function.id}",
            "storage_container_type": "blobContainer",
            "storage_container_endpoint": f"https://{settings['storage']}.blob.core.windows.net/function-packages",
            "storage_authentication_type": "SystemAssignedIdentity",
            "runtime_name": "python", "runtime_version": "3.12",
            "maximum_instance_count": 40, "instance_memory_in_mb": 512,
            "app_settings": app_settings,
            "identity": [{"type": "SystemAssigned"}],
            "site_config": [{"application_insights_connection_string": "${azurerm_application_insights.function.connection_string}"}],
            "tags": {**TAGS, "pdt-app": app["name"]},
        }),
        _resource("azurerm_role_assignment", "function_key_vault_user", {
            "scope": resource_id(settings, "Microsoft.KeyVault", "vaults", settings["vault"]),
            "role_definition_name": "Key Vault Secrets User",
            "principal_id": "${azurerm_function_app_flex_consumption.function.identity[0].principal_id}",
            "principal_type": "ServicePrincipal",
        }),
        _resource("azurerm_role_assignment", "function_storage_blob", {
            "scope": resource_id(settings, "Microsoft.Storage", "storageAccounts", settings["storage"]),
            "role_definition_name": "Storage Blob Data Contributor",
            "principal_id": "${azurerm_function_app_flex_consumption.function.identity[0].principal_id}",
            "principal_type": "ServicePrincipal",
        }),
    )
    return configuration("azurerm", provider_settings(settings), resources)


def container_app_configuration(app: dict, settings: dict[str, str], secret_uri: str | None,
                                storage_url: str | None = None) -> dict:
    secrets = []
    if secret_uri:
        secrets.append({"name": "pdt-env", "identity": resource_id(
            settings, "Microsoft.ManagedIdentity", "userAssignedIdentities", settings["identity"]),
                        "key_vault_secret_id": secret_uri})
    env = []
    if secret_uri:
        env.append({"name": "PDT_ENV_JSON", "secret_name": "pdt-env"})
    if storage_url:
        env.append({"name": "PDT_STORAGE_URL", "value": storage_url})
    # The store grant names one app in its condition, and Azure keeps one assignment
    # per principal, role, and scope, so a job with a store owns its own identity.
    identity_type = "SystemAssigned, UserAssigned" if storage_url else "UserAssigned"
    resources = _resource("azurerm_container_app_job", "job", {
        "name": app["job"], "resource_group_name": settings["resource_group"],
        "location": settings["region"],
        "container_app_environment_id": resource_id(settings, "Microsoft.App", "managedEnvironments", settings["environment"]),
        "replica_timeout_in_seconds": 1800, "replica_retry_limit": 1,
        "schedule_trigger_config": [{"cron_expression": app["cron"], "parallelism": 1,
                                     "replica_completion_count": 1}],
        "identity": [{"type": identity_type, "identity_ids": [resource_id(
            settings, "Microsoft.ManagedIdentity", "userAssignedIdentities", settings["identity"])]}],
        "registry": [{"server": f"{settings['registry']}.azurecr.io", "identity": resource_id(
            settings, "Microsoft.ManagedIdentity", "userAssignedIdentities", settings["identity"])}],
        "secret": secrets,
        "template": [{"container": [{"name": "app", "image": app["image"], "cpu": 0.5,
                                      "memory": "1Gi", "env": env}]}],
        "tags": {**TAGS, "pdt-app": app["name"]},
    })
    return configuration("azurerm", provider_settings(settings), resources)


def shared_imports(settings: dict[str, str], runtime: str) -> dict[str, str]:
    values = {
        "azurerm_resource_group.pdt": f"/subscriptions/{settings['subscription']}/resourceGroups/{settings['resource_group']}",
        "azurerm_key_vault.pdt": resource_id(settings, "Microsoft.KeyVault", "vaults", settings["vault"]),
        "azurerm_log_analytics_workspace.pdt": resource_id(settings, "Microsoft.OperationalInsights", "workspaces", settings["workspace"]),
    }
    if runtime == "functions":
        values["azurerm_storage_account.pdt"] = resource_id(settings, "Microsoft.Storage", "storageAccounts", settings["storage"])
    if runtime == "container_apps":
        values.update({
            "azurerm_container_registry.pdt": resource_id(settings, "Microsoft.ContainerRegistry", "registries", settings["registry"]),
            "azurerm_user_assigned_identity.runner": resource_id(settings, "Microsoft.ManagedIdentity", "userAssignedIdentities", settings["identity"]),
            "azurerm_container_app_environment.pdt": resource_id(settings, "Microsoft.App", "managedEnvironments", settings["environment"]),
        })
    return values


def function_imports(app: dict, settings: dict[str, str]) -> dict[str, str]:
    return {
        "azurerm_function_app_flex_consumption.function": resource_id(settings, "Microsoft.Web", "sites", app["function_app"]),
        "azurerm_application_insights.function": resource_id(settings, "Microsoft.Insights", "components", app["function_app"]),
    }


def container_app_imports(app: dict, settings: dict[str, str]) -> dict[str, str]:
    return {"azurerm_container_app_job.job": resource_id(settings, "Microsoft.App", "jobs", app["job"])}
