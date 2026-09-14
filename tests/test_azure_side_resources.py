import pytest

from pdt import deploy_azure, deploy_azure_functions

RG = "pdt-verify"
APP = "azure-functions-a"
FUNCTION_APP = "pdt-azure-functions-a-d5b684"
SITE_ID = f"/rg/{RG}/sites/{FUNCTION_APP}"
PLAN_ID = f"/rg/{RG}/serverfarms/ASP-pdtverify-0cbd"
OWNER = ("managed-by=pdt", f"pdt-app={APP}")


def reads(monkeypatch, answers):
    """Answer az_json from answers, keyed by the first two words of the command."""
    def fake_az(*args):
        answer = answers.get(args[:2])
        return answer(*args) if callable(answer) else answer

    monkeypatch.setattr(deploy_azure, "az_json", fake_az)
    monkeypatch.setattr(deploy_azure_functions, "az_json", fake_az)


def writes(monkeypatch):
    calls = []
    monkeypatch.setattr(deploy_azure, "run_quiet",
                        lambda *args, **kwargs: calls.append(args) or "")
    monkeypatch.setattr(deploy_azure_functions, "run_quiet",
                        lambda *args, **kwargs: calls.append(args) or "")
    return calls


def test_the_plan_comes_from_the_function_app_when_azure_names_it(monkeypatch):
    reads(monkeypatch, {("functionapp", "show"): {"id": SITE_ID, "serverFarmId": PLAN_ID}})
    assert deploy_azure_functions.plan_id(RG, FUNCTION_APP) == PLAN_ID


def test_flex_consumption_hides_the_plan_so_the_resource_read_finds_it(monkeypatch):
    reads(monkeypatch, {
        ("functionapp", "show"): {"id": SITE_ID},
        ("resource", "show"): PLAN_ID,
    })
    assert deploy_azure_functions.plan_id(RG, FUNCTION_APP) == PLAN_ID


def test_a_missing_function_app_has_no_plan(monkeypatch):
    reads(monkeypatch, {("functionapp", "show"): None})
    assert deploy_azure_functions.plan_id(RG, FUNCTION_APP) == ""


def test_the_plan_azure_hid_is_still_tagged_for_this_app(monkeypatch):
    reads(monkeypatch, {
        ("functionapp", "show"): {"id": SITE_ID},
        ("resource", "show"): PLAN_ID,
    })
    calls = writes(monkeypatch)
    deploy_azure_functions.adopt_side_resources({"resource_group": RG}, FUNCTION_APP, APP)
    assert ("resource", "tag", "--ids", PLAN_ID, "--tags", *OWNER) in calls


def test_a_plan_azure_will_not_name_stops_the_deploy(monkeypatch, capsys):
    reads(monkeypatch, {
        ("functionapp", "show"): {"id": SITE_ID},
        ("resource", "show"): None,
    })
    calls = writes(monkeypatch)
    with pytest.raises(SystemExit):
        deploy_azure_functions.adopt_side_resources({"resource_group": RG}, FUNCTION_APP, APP)
    assert calls == []
    assert "run the same command again" in capsys.readouterr().out


def test_a_plan_holding_two_apps_is_not_this_app_to_delete(monkeypatch):
    reads(monkeypatch, {
        ("functionapp", "show"): {"id": SITE_ID, "serverFarmId": PLAN_ID},
        ("resource", "show"): {"id": PLAN_ID, "properties": {"numberOfSites": 2}},
    })
    assert deploy_azure_functions.app_plan(RG, FUNCTION_APP, None) is None


def test_the_action_group_is_created_tagged_before_azure_makes_its_own(monkeypatch):
    reads(monkeypatch, {("resource", "show"): None})
    calls = writes(monkeypatch)
    deploy_azure.ensure_action_group(RG)
    assert len(calls) == 1
    created = calls[0]
    assert created[:3] == ("monitor", "action-group", "create")
    assert deploy_azure.SMART_ACTION_GROUP in created
    assert created[-2:] == ("--tags", "managed-by=pdt")
    for _, role in deploy_azure.SMART_ACTION_ROLES:
        assert role in created


def test_an_action_group_that_already_exists_is_left_as_it_is(monkeypatch):
    reads(monkeypatch, {("resource", "show"): {"id": "/rg/pdt-verify/actiongroups/smart"}})
    calls = writes(monkeypatch)
    deploy_azure.ensure_action_group(RG)
    assert calls == []
