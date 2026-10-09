import subprocess

import pytest

from pdt import deploy_azure, deploy_azure_container_apps

SUBSCRIPTION = "11111111-1111-1111-1111-111111111111"
OWN = deploy_azure.Environment("my-group", "my-env", False)
SHARED = deploy_azure.Environment("pdt-shared", "pdt-eastus2", True)
TAGGED = {"tags": {"managed-by": "pdt"}, "location": "East US 2"}


def settings_for(platform: dict, monkeypatch) -> dict:
    monkeypatch.delenv("PDT_AZURE_CONTAINER_APPS_ENVIRONMENT", raising=False)
    return deploy_azure.azure_settings({"platform": {
        "provider": "azure", "subscription": SUBSCRIPTION, "region": "eastus2", **platform}})


def test_the_default_environment_is_shared_per_region(monkeypatch):
    settings = settings_for({}, monkeypatch)
    assert settings["environment"] == SHARED
    assert settings["environment"].resource_id(SUBSCRIPTION) == (
        f"/subscriptions/{SUBSCRIPTION}/resourceGroups/pdt-shared"
        "/providers/Microsoft.App/managedEnvironments/pdt-eastus2")


def test_a_configured_environment_is_the_users_own(monkeypatch):
    assert settings_for({"environment": "my-group/my-env"}, monkeypatch)["environment"] == OWN


def test_the_environment_variable_names_the_users_own(monkeypatch):
    monkeypatch.setenv("PDT_AZURE_CONTAINER_APPS_ENVIRONMENT", "my-group/my-env")
    settings = deploy_azure.azure_settings({"platform": {"provider": "azure"}})
    assert settings["environment"] == OWN


def deploy_settings(environment) -> dict:
    return {
        "subscription": SUBSCRIPTION, "resource_group": "pdt", "region": "eastus2",
        "registry": "pdtregistry", "environment": environment, "identity": "pdt-runner",
        "workspace": "pdt-logs", "vault": "pdt-vault", "deployer_object_id": "d",
        "deployer_principal_type": "User", "suffix": "abc1234def",
    }


def plan_for(monkeypatch, settings, resources):
    calls = []

    def read(*args):
        calls.append(args)
        return resources(args)

    plans = []
    monkeypatch.setattr(deploy_azure_container_apps, "preflight", lambda app, requested: settings)
    monkeypatch.setattr(deploy_azure_container_apps, "azure_settings", lambda app: {})
    monkeypatch.setattr(deploy_azure_container_apps, "gather_secrets", lambda app: {})
    monkeypatch.setattr(deploy_azure_container_apps, "check_shared_names", lambda settings: None)
    monkeypatch.setattr(deploy_azure_container_apps, "az_json", read)
    monkeypatch.setattr(deploy_azure, "az_json", read)
    monkeypatch.setattr(deploy_azure_container_apps, "secret_state", lambda *args: (False, None))
    monkeypatch.setattr(deploy_azure_container_apps, "image_action", lambda app, text: text)
    monkeypatch.setattr(deploy_azure_container_apps, "cost_estimate_for", lambda *args: None)
    monkeypatch.setattr(deploy_azure_container_apps, "confirm",
                        lambda actions, *args: plans.append(actions) and False)
    return plans, calls


def test_a_user_environment_creates_no_workspace_and_no_shared_group(monkeypatch):
    plans, calls = plan_for(monkeypatch, deploy_settings(OWN),
                            lambda args: TAGGED if args[:3] == ("containerapp", "env", "show") else None)
    assert deploy_azure_container_apps.deploy(
        {"name": "report", "schedule": "0 0 * * *", "storage": False, "platform": {}, "pause": False}, True) == 1
    plan = "\n".join(plans[0])
    assert "use your own Container Apps environment my-group/my-env" in plan
    assert "workspace" not in plan
    assert "pdt-shared" not in plan
    assert not any("log-analytics" in args for args in calls)


def test_a_missing_user_environment_fails_before_the_plan(monkeypatch, capsys):
    plans, _ = plan_for(monkeypatch, deploy_settings(OWN), lambda args: None)
    with pytest.raises(SystemExit):
        deploy_azure_container_apps.deploy(
            {"name": "report", "schedule": "0 0 * * *", "storage": False, "platform": {}, "pause": False}, True)
    assert plans == []
    assert "my-group/my-env" in capsys.readouterr().out


def test_a_user_environment_in_another_region_fails_before_the_plan(monkeypatch, capsys):
    plans, _ = plan_for(monkeypatch, deploy_settings(OWN),
                        lambda args: TAGGED | {"location": "West US"})
    with pytest.raises(SystemExit):
        deploy_azure_container_apps.deploy(
            {"name": "report", "schedule": "0 0 * * *", "storage": False, "platform": {}, "pause": False}, True)
    assert plans == []
    assert "westus" in capsys.readouterr().out


def test_the_default_plan_creates_the_shared_group_workspace_and_environment(monkeypatch):
    plans, _ = plan_for(monkeypatch, deploy_settings(SHARED), lambda args: None)
    deploy_azure_container_apps.deploy(
        {"name": "report", "schedule": "0 0 * * *", "storage": False, "platform": {}, "pause": False}, True)
    plan = plans[0]
    assert "create resource group pdt-shared (shared by every pdt project in this subscription)" in plan
    assert "create Log Analytics workspace pdt-logs in pdt-shared" in plan
    assert any(line.startswith("create Container Apps environment pdt-shared/pdt-eastus2") for line in plan)
    assert plan.index("create resource group pdt-shared (shared by every pdt project in this subscription)") < plan.index("create resource group pdt")


def job_in(group: str, environment) -> dict:
    return {"resourceGroup": group,
            "properties": {"environmentId": environment.resource_id(SUBSCRIPTION)}}


def release_with(monkeypatch, jobs, other_environments=()):
    def read(*args):
        if args[:3] == ("containerapp", "env", "show"):
            return TAGGED
        if args[:3] == ("containerapp", "job", "list"):
            return jobs
        if args[:2] == ("resource", "list"):
            return [{"name": name} for name in other_environments]
        return None

    monkeypatch.setattr(deploy_azure_container_apps, "az_json", read)
    return deploy_azure_container_apps.environment_release(deploy_settings(SHARED))


def test_destroy_releases_the_environment_and_the_shared_group_when_nothing_uses_them(monkeypatch):
    release = release_with(monkeypatch, [job_in("pdt", SHARED)])
    assert release == deploy_azure_container_apps.Release(environment=True, group=True)
    assert deploy_azure_container_apps.release_actions(deploy_settings(SHARED), release) == [
        "delete Container Apps environment pdt-shared/pdt-eastus2 (no other job uses it)",
        "delete resource group pdt-shared and the Log Analytics workspace pdt-logs in it",
    ]


def test_destroy_releases_the_shared_group_when_the_environment_is_already_gone(monkeypatch):
    def read(*args):
        if args[:2] == ("group", "show"):
            return TAGGED
        return None if args[:3] == ("containerapp", "env", "show") else []

    monkeypatch.setattr(deploy_azure_container_apps, "az_json", read)
    release = deploy_azure_container_apps.environment_release(deploy_settings(SHARED))
    assert release == deploy_azure_container_apps.Release(group=True)


def test_destroy_keeps_the_shared_group_while_another_region_has_an_environment(monkeypatch):
    release = release_with(monkeypatch, [], other_environments=["pdt-westus"])
    assert release == deploy_azure_container_apps.Release(environment=True, group=False)


def test_destroy_keeps_the_environment_another_project_uses(monkeypatch):
    release = release_with(monkeypatch, [job_in("pdt", SHARED), job_in("pdt-other", SHARED),
                                         job_in("pdt-other", OWN)])
    assert release == deploy_azure_container_apps.Release(
        note="Container Apps environment pdt-shared/pdt-eastus2 still runs 1 job "
             "in resource group pdt-other; keeping it")


def test_destroy_never_touches_a_user_environment(monkeypatch):
    monkeypatch.setattr(deploy_azure_container_apps, "az_json",
                        lambda *args: pytest.fail("looked at the environment"))
    release = deploy_azure_container_apps.environment_release(deploy_settings(OWN))
    assert release == deploy_azure_container_apps.Release(
        note="Container Apps environment my-group/my-env is your own; pdt leaves it as it is")


def test_destroy_with_the_project_group_already_gone_still_releases_the_environment(monkeypatch):
    monkeypatch.setattr(deploy_azure, "az", lambda *args: subprocess.CompletedProcess(args, 1, "", ""))
    plans, deleted = [], []
    monkeypatch.setattr(deploy_azure_container_apps, "preflight",
                        lambda app, requested: deploy_settings(SHARED))
    monkeypatch.setattr(deploy_azure_container_apps, "azure_settings", lambda app: {})
    monkeypatch.setattr(deploy_azure_container_apps, "az_json", lambda *args: None)
    monkeypatch.setattr(deploy_azure_container_apps, "az_tsv", lambda *args: "false")
    monkeypatch.setattr(deploy_azure_container_apps, "managed_secret", lambda *args: False)
    monkeypatch.setattr(deploy_azure_container_apps, "environment_release",
                        lambda settings: deploy_azure_container_apps.Release(environment=True, group=True))
    monkeypatch.setattr(deploy_azure_container_apps, "confirm",
                        lambda actions, *args: plans.append(actions) or True)
    monkeypatch.setattr(deploy_azure_container_apps, "destroy_group",
                        lambda settings, name: pytest.fail("deleted a group that does not exist"))
    monkeypatch.setattr(deploy_azure_container_apps, "delete_unless_locked",
                        lambda *args: deleted.append(args[:2]) or "")
    assert deploy_azure_container_apps.destroy({"name": "report", "storage": False}, True) == 0
    assert plans[0][0].startswith("delete Container Apps environment")
    assert deleted == [("containerapp", "env"), ("group", "delete")]


def test_destroy_with_the_project_group_already_gone_purges_its_soft_deleted_vault(monkeypatch):
    monkeypatch.setattr(deploy_azure, "az", lambda *args: subprocess.CompletedProcess(args, 1, "", ""))
    plans, purged = [], []
    deleted_vault = {"properties": {"tags": {"managed-by": "pdt"}}}
    monkeypatch.setattr(deploy_azure_container_apps, "preflight",
                        lambda app, requested: deploy_settings(OWN))
    monkeypatch.setattr(deploy_azure_container_apps, "azure_settings", lambda app: {})
    monkeypatch.setattr(
        deploy_azure_container_apps, "az_json",
        lambda *args: deleted_vault if args[:2] == ("keyvault", "show-deleted") else None)
    monkeypatch.setattr(deploy_azure_container_apps, "az_tsv", lambda *args: "false")
    monkeypatch.setattr(deploy_azure_container_apps, "managed_secret", lambda *args: False)
    monkeypatch.setattr(deploy_azure_container_apps, "confirm",
                        lambda actions, *args: plans.append(actions) or True)
    monkeypatch.setattr(deploy_azure_container_apps, "run_quiet",
                        lambda *args, **kwargs: purged.append(args) or "")
    assert deploy_azure_container_apps.destroy({"name": "report", "storage": False}, True) == 0
    assert plans == [["purge the soft-deleted Key Vault pdt-vault"]]
    assert purged == [("keyvault", "purge", "--name", "pdt-vault")]


def test_destroy_leaves_a_soft_deleted_vault_pdt_did_not_make(monkeypatch):
    monkeypatch.setattr(deploy_azure, "az", lambda *args: subprocess.CompletedProcess(args, 1, "", ""))
    monkeypatch.setattr(deploy_azure_container_apps, "preflight",
                        lambda app, requested: deploy_settings(OWN))
    monkeypatch.setattr(deploy_azure_container_apps, "azure_settings", lambda app: {})
    monkeypatch.setattr(
        deploy_azure_container_apps, "az_json",
        lambda *args: {"properties": {"tags": {}}} if args[:2] == ("keyvault", "show-deleted") else None)
    monkeypatch.setattr(deploy_azure_container_apps, "az_tsv", lambda *args: "false")
    monkeypatch.setattr(deploy_azure_container_apps, "managed_secret", lambda *args: False)
    monkeypatch.setattr(deploy_azure_container_apps, "run_quiet",
                        lambda *args, **kwargs: pytest.fail(f"ran {args}"))
    assert deploy_azure_container_apps.destroy({"name": "report", "storage": False}, True) == 0


def test_the_quota_error_gets_a_hint(monkeypatch, capsys):
    stderr = ('ERROR: (EnvironmentsInSubExceeded) Subscription is over quota '
              'for Managed Environments. Current usage: 1, allowed: 1')
    monkeypatch.setattr(deploy_azure, "az",
                        lambda *command: subprocess.CompletedProcess(command, 1, "", stderr))
    with pytest.raises(SystemExit):
        deploy_azure.run_quiet("containerapp", "env", "create",
                               hints={"EnvironmentsInSubExceeded": deploy_azure_container_apps.QUOTA_HINT})
    out = capsys.readouterr().out
    assert "platform.environment: <resource-group>/<name>" in out
    assert "quota increase" in out


def test_another_error_gets_no_hint(monkeypatch, capsys):
    monkeypatch.setattr(deploy_azure, "az",
                        lambda *command: subprocess.CompletedProcess(command, 1, "", "boom"))
    with pytest.raises(SystemExit):
        deploy_azure.run_quiet("containerapp", "env", "create",
                               hints={"EnvironmentsInSubExceeded": deploy_azure_container_apps.QUOTA_HINT})
    assert "quota" not in capsys.readouterr().out
