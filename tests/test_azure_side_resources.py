from pdt import deploy_azure

RG = "pdt-verify"


def reads(monkeypatch, answers):
    """Answer az_json from answers, keyed by the first two words of the command."""
    def fake_az(*args):
        answer = answers.get(args[:2])
        return answer(*args) if callable(answer) else answer

    monkeypatch.setattr(deploy_azure, "az_json", fake_az)


def writes(monkeypatch):
    calls = []
    monkeypatch.setattr(deploy_azure, "run_quiet",
                        lambda *args, **kwargs: calls.append(args) or "")
    return calls


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
