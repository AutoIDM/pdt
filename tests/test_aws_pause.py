import json

import pytest

from pdt import deploy_aws
from pdt import deploy_aws_batch as batch_deploy

TARGET = {
    "Arn": batch_deploy.SUBMIT_JOB_TARGET,
    "RoleArn": "arn:aws:iam::1:role/pdt-my-app-scheduler",
    "RetryPolicy": {"MaximumRetryAttempts": 1},
    "Input": json.dumps({"JobDefinition": "pdt-my-app", "JobName": "pdt-my-app",
                         "JobQueue": "pdt"}, sort_keys=True),
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


class FakeBatch:
    def __init__(self):
        self.submitted = []

    def submit_job(self, **request):
        self.submitted.append(request)
        return {"jobId": "abc123", "jobName": request["jobName"]}


class FakeSession:
    def __init__(self, scheduler, batch=None):
        self.clients = {"scheduler": scheduler, "batch": batch}

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
    assert batch_deploy.pause(app, FakeSession(scheduler), True) == 0
    assert scheduler.updated == [{
        "Name": "pdt-my-app", "GroupName": "pdt", "ScheduleExpression": "cron(0 6 * * ? *)",
        "ScheduleExpressionTimezone": "Etc/UTC", "FlexibleTimeWindow": {"Mode": "OFF"},
        "Target": TARGET, "Description": "Managed by PDT", "State": "DISABLED"}]
    assert "Paused my-app" in capsys.readouterr().out


def test_pause_leaves_a_schedule_already_in_that_state_alone():
    scheduler = FakeScheduler(schedule("DISABLED"))
    assert batch_deploy.pause({"name": "my-app"}, FakeSession(scheduler), True) == 0
    assert scheduler.updated == []
    assert batch_deploy.pause({"name": "my-app"}, FakeSession(scheduler), False) == 0
    assert scheduler.updated[0]["State"] == "ENABLED"


def test_pause_without_a_schedule_says_deploy_first(capsys):
    with pytest.raises(SystemExit):
        batch_deploy.pause({"name": "my-app"}, FakeSession(FakeScheduler(None)), True)
    assert "run pdt deploy my-app first" in capsys.readouterr().out


def test_start_submits_the_job_the_schedule_would_submit(capsys):
    batch = FakeBatch()
    session = FakeSession(FakeScheduler(schedule("ENABLED")), batch)
    assert batch_deploy.start({"name": "my-app"}, session) == 0
    request = batch.submitted[0]
    assert request["jobName"] == "pdt-my-app"
    assert request["jobQueue"] == batch_deploy.JOB_QUEUE.name
    assert request["jobDefinition"] == "pdt-my-app"
    assert request["tags"] == {"managed-by": "pdt"}
    out = capsys.readouterr().out
    assert "abc123" in out
    assert "pdt runs my-app" in out


def test_the_deployer_policy_allows_submitting_a_job():
    assert "batch:SubmitJob" in batch_deploy.DEPLOYER_ACTIONS
    assert "iam:PassRole" in batch_deploy.DEPLOYER_ACTIONS
