import json
from fnmatch import fnmatchcase

import pytest

from pdt.deploy_aws import deployer_policy, preflight
from pdt.deploy_aws_fargate import DEPLOYER_ACTIONS

ACCOUNT = "123456789012"
REGION = "us-east-1"
CALLER = f"arn:aws:iam::{ACCOUNT}:user/operator"
# Actions AWS cannot scope to a resource ARN, so they must stay on "*".
UNSCOPED = {
    "ec2:DescribeSecurityGroups", "ec2:DescribeSubnets", "ec2:DescribeVpcs",
    "ecr:GetAuthorizationToken", "ecs:RegisterTaskDefinition",
    "ecs:DeregisterTaskDefinition", "ecs:DescribeTaskDefinition",
    "ecs:ListTaskDefinitionFamilies", "ecs:ListTaskDefinitions", "ecs:ListTasks",
    "logs:DescribeLogGroups", "scheduler:ListSchedules",
}
CALLER_ARNS = [f"arn:aws:iam::{ACCOUNT}:user/*", f"arn:aws:iam::{ACCOUNT}:role/*"]
BROAD_POLICY = {"Version": "2012-10-17", "Statement": [
    {"Effect": "Allow", "Action": DEPLOYER_ACTIONS, "Resource": "*"}]}


def policy() -> dict:
    return deployer_policy(DEPLOYER_ACTIONS, ACCOUNT, REGION)


def listed(value) -> list[str]:
    return [value] if isinstance(value, str) else value


def test_the_policy_is_valid_iam_json():
    document = json.loads(json.dumps(policy()))
    assert document["Version"] == "2012-10-17"
    for statement in document["Statement"]:
        assert statement["Effect"] == "Allow"
        assert listed(statement["Action"]) and listed(statement["Resource"])


def test_every_deployer_action_is_granted():
    granted = {action for s in policy()["Statement"] for action in listed(s["Action"])}
    assert granted == set(DEPLOYER_ACTIONS)


def test_only_actions_aws_cannot_scope_use_a_wildcard_resource():
    for statement in policy()["Statement"]:
        if "*" in listed(statement["Resource"]):
            assert set(listed(statement["Action"])) <= UNSCOPED, statement


def test_every_scoped_arn_names_a_pdt_resource():
    for statement in policy()["Statement"]:
        for arn in listed(statement["Resource"]):
            if arn == "*" or arn in CALLER_ARNS:
                continue
            assert "pdt" in arn, statement
            assert ACCOUNT in arn or arn.startswith("arn:aws:s3:::"), arn


def test_pass_role_reaches_only_pdt_roles_for_ecs_and_the_scheduler():
    [statement] = [s for s in policy()["Statement"] if "iam:PassRole" in listed(s["Action"])]
    assert listed(statement["Action"]) == ["iam:PassRole"]
    assert listed(statement["Resource"]) == [f"arn:aws:iam::{ACCOUNT}:role/pdt-*"]
    services = statement["Condition"]["StringEquals"]["iam:PassedToService"]
    assert sorted(services) == ["ecs-tasks.amazonaws.com", "scheduler.amazonaws.com"]


class Sts:
    def get_caller_identity(self):
        return {"Account": ACCOUNT, "Arn": CALLER}


class PolicyIam:
    """Answers SimulatePrincipalPolicy the way IAM does for a login holding `granted`."""

    def __init__(self, granted: dict):
        self.statements = granted["Statement"]

    def allows(self, action: str, resource: str, context: dict) -> bool:
        for statement in self.statements:
            if not any(fnmatchcase(action, p) for p in listed(statement["Action"])):
                continue
            if not any(fnmatchcase(resource, p) for p in listed(statement["Resource"])):
                continue
            wanted = statement.get("Condition", {}).get("StringEquals", {})
            if all(set(context.get(key, [])) & set(values) for key, values in wanted.items()):
                return True
        return False

    def simulate_principal_policy(self, PolicySourceArn, ActionNames,
                                  ResourceArns=("*",), ContextEntries=()):
        context = {entry["ContextKeyName"]: entry["ContextKeyValues"] for entry in ContextEntries}
        return {"EvaluationResults": [
            {"EvalActionName": action,
             "EvalDecision": "allowed" if all(self.allows(action, arn, context)
                                              for arn in ResourceArns) else "implicitDeny"}
            for action in ActionNames]}


def test_a_login_holding_exactly_the_printed_policy_passes_preflight():
    assert preflight(Sts(), PolicyIam(policy()), ACCOUNT, REGION, DEPLOYER_ACTIONS) == (
        ACCOUNT, CALLER)


def test_a_login_holding_the_old_broad_policy_still_passes_preflight():
    assert preflight(Sts(), PolicyIam(BROAD_POLICY), ACCOUNT, REGION, DEPLOYER_ACTIONS) == (
        ACCOUNT, CALLER)


def test_a_login_missing_a_scoped_grant_is_shown_the_policy(capsys):
    granted = policy()
    granted["Statement"] = [s for s in granted["Statement"]
                            if "secretsmanager:CreateSecret" not in listed(s["Action"])]
    with pytest.raises(SystemExit):
        preflight(Sts(), PolicyIam(granted), ACCOUNT, REGION, DEPLOYER_ACTIONS)
    out = capsys.readouterr().out
    assert "secretsmanager:CreateSecret" in out
    assert f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:pdt-*" in out
