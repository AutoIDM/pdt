import sys
import types

import pytest

# deploy_aws imports boto3 at module level; these shapes need no AWS SDK.
if "botocore.exceptions" not in sys.modules:
    sys.modules.setdefault("boto3", types.ModuleType("boto3"))
    exceptions = types.ModuleType("botocore.exceptions")
    exceptions.ClientError = type("ClientError", (Exception,), {})
    botocore = types.ModuleType("botocore")
    botocore.exceptions = exceptions
    sys.modules.setdefault("botocore", botocore)
    sys.modules["botocore.exceptions"] = exceptions

from pdt import deploy_aws, deploy_aws_fargate

TARGET = {
    "Arn": "arn:aws:ecs:us-east-1:1:cluster/pdt",
    "RoleArn": "arn:aws:iam::1:role/pdt-my-app-scheduler",
    "RetryPolicy": {"MaximumRetryAttempts": 1},
    "EcsParameters": {
        "TaskDefinitionArn": "arn:aws:ecs:us-east-1:1:task-definition/pdt-my-app:3",
        "LaunchType": "FARGATE",
        "TaskCount": 1,
        "NetworkConfiguration": {"awsvpcConfiguration": {
            "Subnets": ["subnet-1"], "SecurityGroups": ["sg-1"], "AssignPublicIp": "ENABLED"}},
    },
}


def schedule(state: str) -> dict:
    return {"Name": "pdt-my-app", "GroupName": "pdt", "Arn": "arn:aws:scheduler:...",
            "ScheduleExpression": "cron(0 6 * * ? *)", "ScheduleExpressionTimezone": "Etc/UTC",
            "FlexibleTimeWindow": {"Mode": "OFF"}, "State": state, "Target": TARGET,
            "Description": "Managed by PDT", "CreationDate": "2026-09-23"}


class FakeScheduler:
    def __init__(self, existing: dict | None):
        self.existing = existing
        self.updated = []
        self.created = []

    def get_schedule_group(self, Name):
        return {}

    def get_schedule(self, Name, GroupName):
        if self.existing is None:
            error = type("ClientError", (Exception,), {})()
            error.response = {"Error": {"Code": "ResourceNotFoundException"}}
            raise error
        return self.existing

    def update_schedule(self, **request):
        self.updated.append(request)

    def create_schedule(self, **request):
        self.created.append(request)


class FakeEcs:
    def __init__(self):
        self.started = []

    def run_task(self, **request):
        self.started.append(request)
        return {"tasks": [{"taskArn": "arn:aws:ecs:us-east-1:1:task/pdt/abc123"}], "failures": []}


class FakeSession:
    def __init__(self, scheduler, ecs=None):
        self.clients = {"scheduler": scheduler, "ecs": ecs}

    def client(self, name):
        return self.clients[name]


@pytest.fixture(autouse=True)
def no_retry_wait(monkeypatch):
    monkeypatch.setattr(deploy_aws, "with_role_propagation_retry", lambda call: call())


def test_ensure_schedule_creates_a_paused_schedule_disabled():
    scheduler = FakeScheduler(None)
    deploy_aws.ensure_schedule(scheduler, "pdt-my-app", "cron(0 6 * * ? *)", "Etc/UTC",
                               TARGET["RoleArn"], {"Arn": TARGET["Arn"]}, paused=True)
    assert scheduler.created[0]["State"] == "DISABLED"
    deploy_aws.ensure_schedule(scheduler, "pdt-my-app", "cron(0 6 * * ? *)", "Etc/UTC",
                               TARGET["RoleArn"], {"Arn": TARGET["Arn"]})
    assert scheduler.created[1]["State"] == "ENABLED"


def test_pause_updates_the_schedule_with_its_own_fields_and_the_new_state(capsys):
    scheduler = FakeScheduler(schedule("ENABLED"))
    app = {"name": "my-app"}
    assert deploy_aws_fargate.pause(app, FakeSession(scheduler), True) == 0
    assert scheduler.updated == [{
        "Name": "pdt-my-app", "GroupName": "pdt", "ScheduleExpression": "cron(0 6 * * ? *)",
        "ScheduleExpressionTimezone": "Etc/UTC", "FlexibleTimeWindow": {"Mode": "OFF"},
        "Target": TARGET, "Description": "Managed by PDT", "State": "DISABLED"}]
    assert "Paused my-app" in capsys.readouterr().out


def test_pause_leaves_a_schedule_already_in_that_state_alone():
    scheduler = FakeScheduler(schedule("DISABLED"))
    assert deploy_aws_fargate.pause({"name": "my-app"}, FakeSession(scheduler), True) == 0
    assert scheduler.updated == []
    assert deploy_aws_fargate.pause({"name": "my-app"}, FakeSession(scheduler), False) == 0
    assert scheduler.updated[0]["State"] == "ENABLED"


def test_pause_without_a_schedule_says_deploy_first(capsys):
    with pytest.raises(SystemExit):
        deploy_aws_fargate.pause({"name": "my-app"}, FakeSession(FakeScheduler(None)), True)
    assert "run pdt deploy my-app first" in capsys.readouterr().out


def test_start_runs_the_task_the_schedule_would_run(capsys):
    ecs = FakeEcs()
    session = FakeSession(FakeScheduler(schedule("ENABLED")), ecs)
    assert deploy_aws_fargate.start({"name": "my-app"}, session) == 0
    request = ecs.started[0]
    assert request["cluster"] == TARGET["Arn"]
    assert request["taskDefinition"] == TARGET["EcsParameters"]["TaskDefinitionArn"]
    assert request["launchType"] == "FARGATE"
    assert request["count"] == 1
    assert request["networkConfiguration"] == {"awsvpcConfiguration": {
        "subnets": ["subnet-1"], "securityGroups": ["sg-1"], "assignPublicIp": "ENABLED"}}
    assert {"key": "managed-by", "value": "pdt"} in request["tags"]
    out = capsys.readouterr().out
    assert "abc123" in out
    assert "pdt runs my-app" in out


def test_the_deployer_policy_allows_run_task():
    assert "ecs:RunTask" in deploy_aws_fargate.DEPLOYER_ACTIONS
    assert "iam:PassRole" in deploy_aws_fargate.DEPLOYER_ACTIONS
