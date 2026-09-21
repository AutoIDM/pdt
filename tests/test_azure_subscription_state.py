from pdt import deploy_azure

SUB = {"id": "79ee0bd6-0000-0000-0000-000000000000", "name": "Pay-As-You-Go"}


def test_an_enabled_subscription_has_no_problem():
    assert deploy_azure.subscription_problem({**SUB, "state": "Enabled"}) == ""


def test_a_past_due_subscription_still_accepts_writes():
    assert deploy_azure.subscription_problem({**SUB, "state": "PastDue"}) == ""


def test_a_missing_state_is_not_treated_as_a_problem():
    assert deploy_azure.subscription_problem(SUB) == ""


def test_a_disabled_subscription_names_the_state_and_the_fix():
    message = deploy_azure.subscription_problem({**SUB, "state": "Disabled"})
    assert "Pay-As-You-Go" in message
    assert SUB["id"] in message
    assert "Disabled" in message
    assert deploy_azure.SUBSCRIPTIONS_URL in message
    assert "platform.subscription" in message


def test_warned_expired_and_deleted_are_problems():
    for state in ("Warned", "Expired", "Deleted"):
        assert state in deploy_azure.subscription_problem({**SUB, "state": state})
