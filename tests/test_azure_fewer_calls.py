import pytest

from conftest import built_image, fake_azure_reads
from pdt import deploy_azure
from pdt import deploy_azure_container_apps as azure

SUBSCRIPTION = "11111111-1111-1111-1111-111111111111"
SHARED = deploy_azure.Environment("pdt-shared", "pdt-eastus2", True)
APP = {"name": "report", "schedule": "0 0 * * *", "storage": False, "platform": {}, "pause": False}
MANAGED = {"tags": {"managed-by": "pdt"}}
GROUP = MANAGED | {"location": "eastus2"}
ARM_AUTH = {"properties": {"policies": {"azureADAuthenticationAsArmPolicy": {"status": "enabled"}}}}
JOB = {"tags": {"managed-by": "pdt", "pdt-app": "report"},
       "identity": {"principalId": "job-principal"},
       "properties": {"runningState": "Ready"}}
ASSIGNED = [{"id": "/assignment", "condition": None}]
PRESENT = {
    ("group", "show"): GROUP,
    ("acr", "show"): MANAGED | ARM_AUTH,
    ("containerapp", "env", "show"): MANAGED,
    ("identity", "show"): MANAGED | {"id": "/identity", "properties": {"principalId": "runner"}},
    ("monitor", "log-analytics", "workspace", "show"): MANAGED,
    ("containerapp", "job", "show"): JOB,
    ("lock", "show"): {"name": "held"},
    ("keyvault", "show"): MANAGED,
    ("role", "assignment", "list"): ASSIGNED,
}


def settings() -> dict:
    return {
        "subscription": SUBSCRIPTION, "resource_group": "pdt", "region": "eastus2",
        "registry": "pdtregistry", "environment": SHARED, "identity": "pdt-runner",
        "workspace": "pdt-logs", "vault": "pdt-vault", "deployer_object_id": "deployer",
        "deployer_principal_type": "User", "suffix": "abc1234def", "token": "t",
    }


MISSING = {("identity", "create"): {"id": "/identity", "principalId": "runner"}}


def deploy(monkeypatch, answers, job_after=None):
    """Run deploy against a fake az and return every az command it ran, reads and writes."""
    calls = []

    def answer(args):
        calls.append(args)
        matches = [key for key in answers if args[:len(key)] == key]
        return answers[max(matches, key=len)] if matches else None

    def tsv(*args):
        calls.append(args)
        return "Registered" if args[:2] == ("provider", "show") else "value"

    fake_azure_reads(monkeypatch, lambda *command: answer(command))
    for module in (azure, deploy_azure):
        monkeypatch.setattr(module, "az_json", lambda *args: answer(args))
        monkeypatch.setattr(module, "az_tsv", tsv)
        monkeypatch.setattr(module, "run_quiet", lambda *args, **kwargs: answer(args) or "")
    monkeypatch.setattr(azure, "preflight", lambda app, requested: settings())
    monkeypatch.setattr(azure, "azure_settings", lambda app: {})
    monkeypatch.setattr(azure, "build_in_cloud", lambda *args: False)
    monkeypatch.setattr(azure, "gather_secrets", lambda app: {})
    monkeypatch.setattr(azure, "check_shared_names", lambda settings, resources: None)
    monkeypatch.setattr(azure, "image_action", lambda app, text, cloud_builder="": text)
    monkeypatch.setattr(azure, "cost_estimate_for", lambda *args: None)
    monkeypatch.setattr(azure, "confirm", lambda *args: True)
    monkeypatch.setattr(azure, "build_image", lambda *args: None)
    monkeypatch.setattr(azure, "ensure_image", built_image)
    monkeypatch.setattr(azure, "pushed_image", lambda settings, name: f"{name}@sha256:1")
    monkeypatch.setattr(azure, "reconcile_job", lambda *args: job_after)
    assert azure.deploy(APP, True) == 0
    return calls


def ran(calls, *prefix):
    return [call for call in calls if call[:len(prefix)] == prefix]


def test_a_deploy_with_everything_in_place_makes_no_redundant_call(monkeypatch):
    calls = deploy(monkeypatch, PRESENT, job_after=JOB)
    assert ran(calls, "provider") == []
    assert ran(calls, "group", "create") == []
    assert len(ran(calls, "group", "show")) == 2
    assert ran(calls, "monitor", "log-analytics", "workspace", "get-shared-keys") == []
    assert ran(calls, "acr", "config") == []
    assert [call[4] for call in ran(calls, "containerapp", "job", "show")] == [
        azure.job_name(settings(), "report")]
    assert ran(calls, "rest") == []
    assert [call for call in calls if call[:2] == ("role", "assignment")
            and call[2] != "list"] == []


def test_a_first_deploy_registers_providers_and_creates_and_reads_what_it_needs(monkeypatch):
    calls = deploy(monkeypatch, MISSING, job_after=JOB)
    assert [call[3] for call in ran(calls, "provider", "show")] == [
        "Microsoft.KeyVault", "Microsoft.ManagedIdentity", "Microsoft.App",
        "Microsoft.ContainerRegistry", "Microsoft.OperationalInsights"]
    assert [call[3] for call in ran(calls, "group", "create")] == ["pdt-shared", "pdt"]
    assert ran(calls, "monitor", "log-analytics", "workspace", "get-shared-keys")
    assert ran(calls, "containerapp", "env", "create")
    assert ran(calls, "acr", "config", "authentication-as-arm", "update")
    assert [call[4] for call in ran(calls, "containerapp", "job", "show")] == [
        azure.job_name(settings(), "report"), azure.legacy_job_name("report")]
    assert ran(calls, "containerapp", "job", "execution") == []


@pytest.mark.parametrize("missing, provider", [
    (("keyvault", "show"), "Microsoft.KeyVault"),
    (("identity", "show"), "Microsoft.ManagedIdentity"),
    (("acr", "show"), "Microsoft.ContainerRegistry"),
    (("monitor", "log-analytics", "workspace", "show"), "Microsoft.OperationalInsights"),
])
def test_only_a_provider_with_no_resource_found_is_checked(monkeypatch, missing, provider):
    answers = {key: value for key, value in PRESENT.items() if key != missing} | MISSING
    calls = deploy(monkeypatch, answers, job_after=JOB)
    assert [call[3] for call in ran(calls, "provider", "show")] == [provider]


def test_the_app_provider_is_checked_when_neither_the_environment_nor_the_job_exists(
        monkeypatch):
    answers = {key: value for key, value in PRESENT.items()
               if key not in (("containerapp", "env", "show"), ("containerapp", "job", "show"))}
    calls = deploy(monkeypatch, answers | MISSING, job_after=JOB)
    assert [call[3] for call in ran(calls, "provider", "show")] == ["Microsoft.App"]


def test_a_group_in_another_region_goes_to_group_create_so_azure_reports_it(monkeypatch):
    calls = deploy(monkeypatch, PRESENT | {("group", "show"): MANAGED | {"location": "westus"}},
                   job_after=JOB)
    assert [call[3] for call in ran(calls, "group", "create")] == ["pdt-shared", "pdt"]


def test_a_workspace_missing_beside_an_existing_environment_is_created_with_no_key_read(
        monkeypatch):
    answers = {key: value for key, value in PRESENT.items()
               if key != ("monitor", "log-analytics", "workspace", "show")}
    calls = deploy(monkeypatch, answers, job_after=JOB)
    assert ran(calls, "monitor", "log-analytics", "workspace", "create")
    assert ran(calls, "monitor", "log-analytics", "workspace", "get-shared-keys") == []
    assert ran(calls, "containerapp", "env", "create") == []


def test_arm_authentication_is_enabled_only_when_the_registry_has_it_off(monkeypatch):
    off = MANAGED | {"properties": {"policies": {"azureADAuthenticationAsArmPolicy": {
        "status": "disabled"}}}}
    calls = deploy(monkeypatch, PRESENT | {("acr", "show"): off}, job_after=JOB)
    assert ran(calls, "acr", "config", "authentication-as-arm", "update")
    assert not azure.acr_arm_auth_enabled(None)
    assert not azure.acr_arm_auth_enabled(MANAGED)


def test_a_paused_app_suspends_a_ready_job_from_the_state_deploy_read(monkeypatch):
    monkeypatch.setitem(APP, "pause", True)
    calls = deploy(monkeypatch, PRESENT, job_after=JOB)
    assert [call[4].split("?")[0].rsplit("/", 1)[-1]
            for call in ran(calls, "rest", "--method", "post")] == ["suspend"]
    assert ran(calls, "rest", "--method", "get") == []


def test_role_checks_ask_for_one_assignment_by_object_id_and_role_id(monkeypatch):
    calls = []
    monkeypatch.setattr(deploy_azure, "az_json", lambda *args: calls.append(args) or ASSIGNED)
    monkeypatch.setattr(deploy_azure, "run_quiet", lambda *args, **kwargs: calls.append(args))
    deploy_azure.assign_role("/scope", "principal", "AcrPull")
    assert calls == [("role", "assignment", "list", "--assignee-object-id", "principal",
                      "--role", "7f951dda-4ed3-4680-a7ca-43fe172d538d", "--scope", "/scope",
                      "--fill-role-definition-name", "false", "--fill-principal-name", "false")]


def test_an_assignment_with_another_condition_is_replaced_by_its_id(monkeypatch):
    calls = []
    monkeypatch.setattr(deploy_azure, "az_json", lambda *args: calls.append(args) or ASSIGNED)
    monkeypatch.setattr(deploy_azure, "run_quiet", lambda *args, **kwargs: calls.append(args))
    deploy_azure.assign_role("/scope", "principal", deploy_azure.STORE_ROLE, condition="c")
    assert calls[1] == ("role", "assignment", "delete", "--ids", "/assignment")
    assert calls[2][:9] == ("role", "assignment", "create", "--assignee-object-id", "principal",
                            "--assignee-principal-type", "ServicePrincipal", "--role",
                            "ba92f5b4-2d11-453d-a403-e96b0029c9fe")


def test_a_missing_assignment_is_created_and_a_missing_one_is_not_revoked(monkeypatch):
    calls = []
    monkeypatch.setattr(deploy_azure, "az_json", lambda *args: calls.append(args) or [])
    monkeypatch.setattr(deploy_azure, "run_quiet", lambda *args, **kwargs: calls.append(args))
    deploy_azure.revoke_role("/scope", "principal", deploy_azure.SECRET_ROLE)
    assert len(calls) == 1
    deploy_azure.assign_role("/scope", "principal", deploy_azure.SECRET_ROLE)
    assert calls[-1][:3] == ("role", "assignment", "create")


def test_a_held_assignment_is_revoked_by_its_id(monkeypatch):
    calls = []
    monkeypatch.setattr(deploy_azure, "az_json", lambda *args: ASSIGNED)
    monkeypatch.setattr(deploy_azure, "run_quiet", lambda *args, **kwargs: calls.append(args))
    deploy_azure.revoke_role("/scope", "principal", deploy_azure.SECRET_ROLE)
    assert calls == [("role", "assignment", "delete", "--ids", "/assignment")]
