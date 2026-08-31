import base64
import json

import pytest

from conftest import add_app
from pdt import config, deploy_azure, deploy_azure_functions

ACCOUNT = {
    "id": "11111111-1111-1111-1111-111111111111",
    "name": "Pay-As-You-Go",
    "user": {"name": "someone@example.com", "type": "user"},
}
OTHER = {
    "id": "22222222-2222-2222-2222-222222222222",
    "name": "Production",
    "user": {"name": "someone@example.com", "type": "user"},
}
DEPLOYER = "33333333-3333-3333-3333-333333333333"


def jwt_for(claims: dict) -> str:
    segment = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"header.{segment}.signature"


@pytest.fixture
def azure_app(project, monkeypatch):
    monkeypatch.delenv("PDT_AZURE_SUBSCRIPTION", raising=False)
    monkeypatch.delenv("PDT_AZURE_DEPLOYER_OBJECT_ID", raising=False)
    add_app(project, "my-report", "schedule: daily\ntimezone: Etc/UTC\n")
    monkeypatch.setattr(deploy_azure, "az_json", lambda *args: ACCOUNT)
    monkeypatch.setattr(deploy_azure, "access_token",
                        lambda: jwt_for({"oid": DEPLOYER}))
    return config.merged_app("my-report")


def chosen(monkeypatch, result=OTHER):
    calls = []

    def record(app, requested, can_ask):
        calls.append(requested)
        return result

    monkeypatch.setattr(deploy_azure, "choose_subscription", record)
    monkeypatch.setattr(deploy_azure, "run_quiet", lambda *args, **kwargs: "")
    return calls


def saved(monkeypatch):
    calls = []

    def record(app, key, value):
        calls.append(value)
        return config.find_project() / "pdt.yml"

    monkeypatch.setattr(config, "save_platform_key", record)
    return calls


def test_an_unset_subscription_uses_the_active_azure_login(azure_app, monkeypatch):
    calls = chosen(monkeypatch)
    settings = deploy_azure.preflight(azure_app, deploy_azure.azure_settings(azure_app), True)
    assert calls == []
    assert settings["subscription"] == ACCOUNT["id"]


def test_a_placeholder_subscription_uses_the_active_azure_login(project, azure_app, monkeypatch):
    (project / "pdt.yml").write_text(
        f"platform:\n  provider: azure\n  subscription: \"{deploy_azure.PLACEHOLDER_SUBSCRIPTION}\"\n")
    app = config.merged_app("my-report")
    calls = chosen(monkeypatch)
    settings = deploy_azure.preflight(app, deploy_azure.azure_settings(app), True)
    assert calls == []
    assert settings["subscription"] == ACCOUNT["id"]


def test_a_configured_subscription_that_differs_from_the_login_is_looked_up(
        project, azure_app, monkeypatch):
    (project / "pdt.yml").write_text(
        f"platform:\n  provider: azure\n  subscription: \"{OTHER['id']}\"\n")
    app = config.merged_app("my-report")
    calls = chosen(monkeypatch)
    settings = deploy_azure.preflight(app, deploy_azure.azure_settings(app), True)
    assert calls == [OTHER["id"]]
    assert settings["subscription"] == OTHER["id"]


def test_a_configured_subscription_that_matches_the_login_is_not_looked_up(
        project, azure_app, monkeypatch):
    (project / "pdt.yml").write_text(
        f"platform:\n  provider: azure\n  subscription: \"{ACCOUNT['id']}\"\n")
    app = config.merged_app("my-report")
    calls = chosen(monkeypatch)
    deploy_azure.preflight(app, deploy_azure.azure_settings(app), True)
    assert calls == []


def test_an_unattended_run_does_not_write_the_subscription_into_the_project(
        azure_app, monkeypatch):
    chosen(monkeypatch)
    writes = saved(monkeypatch)
    deploy_azure.preflight(azure_app, deploy_azure.azure_settings(azure_app), True)
    assert writes == []


def test_a_person_at_the_keyboard_still_gets_the_subscription_written_back(
        azure_app, monkeypatch):
    chosen(monkeypatch)
    writes = saved(monkeypatch)
    monkeypatch.setattr(deploy_azure, "can_prompt", lambda interactive: True)
    deploy_azure.preflight(azure_app, deploy_azure.azure_settings(azure_app), False)
    assert writes == [ACCOUNT["id"]]


def test_an_unattended_run_with_an_unknown_subscription_names_the_fix(
        project, azure_app, monkeypatch):
    (project / "pdt.yml").write_text(
        "platform:\n  provider: azure\n  subscription: \"not-a-subscription\"\n")
    app = config.merged_app("my-report")
    monkeypatch.setattr(deploy_azure, "az_json", lambda *args: (
        ACCOUNT if args[:2] == ("account", "show") else [ACCOUNT, OTHER]))
    with pytest.raises(SystemExit):
        deploy_azure.preflight(app, deploy_azure.azure_settings(app), True)


@pytest.mark.parametrize("filler", ["", "a", "ab", "abc"])
def test_the_deployer_object_id_comes_out_of_the_access_token(filler):
    token = jwt_for({"oid": DEPLOYER, "appid": filler})
    assert deploy_azure.object_id_from_token(token) == DEPLOYER


def test_a_token_without_an_object_id_claim_gives_an_empty_answer():
    assert deploy_azure.object_id_from_token(jwt_for({"appid": "x"})) == ""


@pytest.mark.parametrize("token", ["", "not-a-token", "one.two", "a.@@@@.c"])
def test_an_unreadable_token_gives_an_empty_answer(token):
    assert deploy_azure.object_id_from_token(token) == ""


def test_preflight_reads_the_deployer_object_id_from_the_token(azure_app, monkeypatch):
    chosen(monkeypatch)
    settings = deploy_azure.preflight(azure_app, deploy_azure.azure_settings(azure_app), True)
    assert settings["deployer_object_id"] == DEPLOYER
    assert settings["deployer_principal_type"] == "User"


def test_the_deployer_object_id_environment_variable_wins(azure_app, monkeypatch):
    chosen(monkeypatch)
    monkeypatch.setenv("PDT_AZURE_DEPLOYER_OBJECT_ID", DEPLOYER)
    monkeypatch.setattr(deploy_azure, "access_token", lambda: jwt_for({"oid": "other"}))
    settings = deploy_azure.preflight(azure_app, deploy_azure.azure_settings(azure_app), True)
    assert settings["deployer_object_id"] == DEPLOYER


def test_a_service_principal_login_is_recorded_as_a_service_principal(azure_app, monkeypatch):
    account = dict(ACCOUNT, user={"name": "systemAssignedIdentity", "type": "servicePrincipal"})
    monkeypatch.setattr(deploy_azure, "az_json", lambda *args: account)
    chosen(monkeypatch)
    settings = deploy_azure.preflight(azure_app, deploy_azure.azure_settings(azure_app), True)
    assert settings["deployer_object_id"] == DEPLOYER
    assert settings["deployer_principal_type"] == "ServicePrincipal"


def test_the_functions_runtime_registers_the_log_and_insights_providers():
    assert "Microsoft.OperationalInsights" in deploy_azure_functions.PROVIDERS
    assert "Microsoft.Insights" in deploy_azure_functions.PROVIDERS
