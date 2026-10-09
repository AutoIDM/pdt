import subprocess

import pytest

from pdt import deploy_azure, deploy_azure_container_apps

SUBSCRIPTION = "11111111-1111-1111-1111-111111111111"
SHARED = deploy_azure.Environment("pdt-shared", "pdt-eastus2", True)
MANAGED = {"tags": {"managed-by": "pdt"}, "location": "East US 2"}
APP = {"name": "report", "schedule": "0 0 * * *", "storage": False, "platform": {}}
LOCKED = (
    "ERROR: (ScopeLocked) The scope '/subscriptions/x/resourceGroups/pdt' cannot perform "
    "delete operation because following scope(s) are locked: '/subscriptions/x/resourceGroups"
    "/pdt/providers/Microsoft.ContainerRegistry/registries/pdtregistry/providers"
    "/Microsoft.Authorization/locks/pdt-other'. Please remove the lock and try again.")


def settings() -> dict:
    return {
        "subscription": SUBSCRIPTION, "resource_group": "pdt", "region": "eastus2",
        "registry": "pdtregistry", "environment": SHARED, "identity": "pdt-runner",
        "workspace": "pdt-logs", "vault": "pdt-vault", "deployer_object_id": "d",
        "deployer_principal_type": "User", "suffix": "abc1234def",
    }


def fake_deploy(monkeypatch, answers):
    """Drive deploy through the plan and the act phase with a fake az.

    Reads come from answers, keyed by the first two words of the command.
    Writes and the image build land in the returned events list, in order.
    """
    events, plans = [], []
    module = deploy_azure_container_apps

    def read(*args):
        return answers.get(args[:2])

    def write(*args, **kwargs):
        events.append(args)
        return ""

    monkeypatch.setattr(module, "preflight", lambda app, requested: settings())
    monkeypatch.setattr(module, "azure_settings", lambda app: {})
    monkeypatch.setattr(module, "gather_secrets", lambda app: {})
    monkeypatch.setattr(module, "check_shared_names", lambda settings: None)
    monkeypatch.setattr(module, "az_json", read)
    monkeypatch.setattr(deploy_azure, "az_json", read)
    monkeypatch.setattr(module, "run_quiet", write)
    monkeypatch.setattr(deploy_azure, "run_quiet", write)
    monkeypatch.setattr(module, "workspace_resource", lambda settings: MANAGED)
    monkeypatch.setattr(module, "acr_arm_auth_enabled", lambda registry: True)
    monkeypatch.setattr(module, "secret_state", lambda *args: (False, None))
    monkeypatch.setattr(module, "image_action", lambda app, text: text)
    monkeypatch.setattr(module, "cost_estimate_for", lambda *args: None)
    monkeypatch.setattr(module, "confirm", lambda actions, *args: plans.append(actions) or True)
    monkeypatch.setattr(module, "ensure_environment", lambda *args: None)
    monkeypatch.setattr(module, "ensure_group_and_vault", lambda *args: "/vault")
    monkeypatch.setattr(module, "assign_role", lambda *args: None)
    monkeypatch.setattr(module, "enable_acr_arm_auth", lambda registry: None)
    monkeypatch.setattr(module, "build_image", lambda *args: events.append(("build",)))
    monkeypatch.setattr(module, "ensure_secret", lambda *args: None)
    monkeypatch.setattr(module, "reconcile_job", lambda *args: None)
    monkeypatch.setattr(module, "job_principal_id", lambda *args: "p")
    return events, plans


EXISTING = {
    ("group", "show"): MANAGED,
    ("acr", "show"): MANAGED,
    ("containerapp", "env"): MANAGED,
    ("identity", "show"): MANAGED | {"id": "/identity", "principalId": "p"},
}


def test_deploy_ends_with_the_next_pdt_commands(monkeypatch, capsys):
    fake_deploy(monkeypatch, EXISTING)
    assert deploy_azure_container_apps.deploy(APP, True) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[out.index("Next steps:") + 1:] == [
        "  pdt az containerapp job start --name pdt-report-3cc3425 --resource-group pdt",
        "                     start a run now",
        "  pdt logs report    read the log of the newest run",
        "  pdt runs report    list the recent runs",
        "  pdt health report  show whether the last run succeeded",
    ]


def test_deploy_locks_the_registry_and_the_environment_before_the_build(monkeypatch):
    events, plans = fake_deploy(monkeypatch, EXISTING)
    assert deploy_azure_container_apps.deploy(APP, True) == 0
    plan = plans[0]
    assert "create lock pdt-report on ACR pdtregistry (keeps it while report is deployed)" in plan
    assert ("create lock pdt-report-in-pdt on Container Apps environment pdt-shared/pdt-eastus2 "
            "(keeps it while report is deployed)") in plan
    locks = [event for event in events if event[:2] == ("lock", "create")]
    assert [event[3] for event in locks] == ["pdt-report", "pdt-report-in-pdt"]
    assert all("CanNotDelete" in event for event in locks)
    assert all(events.index(event) < events.index(("build",)) for event in locks)


def test_deploy_leaves_its_own_lock_in_place(monkeypatch):
    events, plans = fake_deploy(monkeypatch, EXISTING | {("lock", "show"): {"name": "pdt-report"}})
    assert deploy_azure_container_apps.deploy(APP, True) == 0
    assert "use existing lock pdt-report on ACR pdtregistry" in plans[0]
    assert not any(event[:2] == ("lock", "create") for event in events)


def fake_destroy(monkeypatch, answers, locked_by):
    events, plans = [], []
    module = deploy_azure_container_apps

    def read(*args):
        return answers.get(args[:3], answers.get(args[:2]))

    def write(*args, **kwargs):
        events.append(args)
        return ""

    def delete(*args):
        events.append(args)
        return locked_by.get(args[:2], "")

    monkeypatch.setattr(module, "preflight", lambda app, requested: settings())
    monkeypatch.setattr(module, "azure_settings", lambda app: {})
    monkeypatch.setattr(module, "az_json", read)
    monkeypatch.setattr(deploy_azure, "az_json", read)
    monkeypatch.setattr(module, "az_tsv", lambda *args: "true")
    monkeypatch.setattr(deploy_azure, "az_tsv", lambda *args: "true")
    monkeypatch.setattr(module, "run_quiet", write)
    monkeypatch.setattr(deploy_azure, "run_quiet", write)
    monkeypatch.setattr(deploy_azure, "delete_unless_locked", delete)
    monkeypatch.setattr(module, "delete_unless_locked", delete)
    monkeypatch.setattr(module, "managed_secret", lambda *args: False)
    monkeypatch.setattr(module, "confirm", lambda actions, *args: plans.append(actions) or True)
    return events, plans


LAST_APP = {
    ("containerapp", "job", "show"): {"tags": {"managed-by": "pdt", "pdt-app": "report"}},
    ("containerapp", "job", "list"): [],
    ("containerapp", "env"): MANAGED,
    ("group", "show"): MANAGED,
    ("resource", "list"): [],
    ("lock", "show"): {"name": "held"},
}


def test_destroy_removes_its_lock_then_keeps_a_group_another_app_locked(monkeypatch, capsys):
    events, plans = fake_destroy(monkeypatch, LAST_APP, {("group", "delete"): "pdt-other"})
    assert deploy_azure_container_apps.destroy(APP, True) == 0
    assert "remove lock pdt-report from ACR pdtregistry" in plans[0]
    assert ("remove lock pdt-report-in-pdt from Container Apps environment "
            "pdt-shared/pdt-eastus2") in plans[0]
    assert [event[:4] for event in events[:2]] == [
        ("lock", "delete", "--name", "pdt-report"),
        ("lock", "delete", "--name", "pdt-report-in-pdt"),
    ]
    assert ("group", "delete", "--name", "pdt", "--yes") in events
    job = deploy_azure_container_apps.job_name(settings(), "report")
    assert ("containerapp", "job", "delete", "--name", job, "--resource-group", "pdt", "--yes") in events
    assert not any(event[:3] == ("containerapp", "env", "delete") for event in events)
    out = capsys.readouterr().out
    assert "kept: resource group pdt" in out
    assert "locked by pdt-other" in out


def test_destroy_does_not_delete_an_environment_a_job_joined_since_the_plan(monkeypatch, capsys):
    joined = {"resourceGroup": "pdt-other",
              "properties": {"environmentId": SHARED.resource_id(SUBSCRIPTION)}}
    monkeypatch.setattr(deploy_azure_container_apps, "az_json",
                        lambda *args: [joined] if args[:3] == ("containerapp", "job", "list") else None)
    monkeypatch.setattr(deploy_azure_container_apps, "delete_unless_locked",
                        lambda *args: pytest.fail(f"deleted with {args}"))
    deploy_azure_container_apps.release_environment(
        settings(), deploy_azure_container_apps.Release(environment=True, group=True))
    assert "still runs 1 job in resource group pdt-other; keeping it" in capsys.readouterr().out


def test_destroy_does_not_delete_a_group_an_app_joined_since_the_plan(monkeypatch, capsys):
    joined = {"name": "pdt-other", "tags": {"managed-by": "pdt", "pdt-app": "other"}}
    monkeypatch.setattr(deploy_azure, "az_json",
                        lambda *args: [joined] if args[:2] == ("resource", "list") else None)
    monkeypatch.setattr(deploy_azure, "delete_unless_locked",
                        lambda *args: pytest.fail(f"deleted with {args}"))
    assert deploy_azure.destroy_group(settings(), "report") is False
    assert "kept: resource group pdt" in capsys.readouterr().out


def test_a_refused_delete_names_the_lock_that_refused_it(monkeypatch):
    monkeypatch.setattr(deploy_azure, "az",
                        lambda *command: subprocess.CompletedProcess(command, 1, "", LOCKED))
    assert deploy_azure.delete_unless_locked("group", "delete", "--name", "pdt", "--yes") == "pdt-other"


def test_any_other_delete_error_still_fails(monkeypatch, capsys):
    monkeypatch.setattr(deploy_azure, "az",
                        lambda *command: subprocess.CompletedProcess(command, 1, "", "boom"))
    with pytest.raises(SystemExit):
        deploy_azure.delete_unless_locked("group", "delete", "--name", "pdt", "--yes")
    assert "boom" in capsys.readouterr().out
