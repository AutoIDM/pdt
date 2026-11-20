import subprocess
import threading

import pytest

import inventory
from verify import baseline_check, wait_for


@pytest.mark.parametrize("operation", ["list-tags-for-resource", "list-schedules"])
def test_a_deleted_schedule_group_is_rechecked_until_it_disappears(monkeypatch, operation):
    listings = [
        {"ScheduleGroups": [{"Name": "pdt", "Arn": "arn:group/pdt"}]},
        {"ScheduleGroups": []},
    ]
    slept = []

    def aws(region, service, action, *args):
        if action == "list-schedule-groups":
            return listings.pop(0)
        if action == operation:
            raise inventory.InventoryError(
                "An error occurred (ResourceNotFoundException): Schedule group pdt does not exist")
        return {"Tags": [{"Key": "managed-by", "Value": "pdt"}]}

    monkeypatch.setattr(inventory, "aws", aws)
    problems = wait_for(lambda: inventory.aws_schedules("us-east-1"), baseline_check(frozenset()),
                        sleep=slept.append, clock=lambda: 0)
    assert problems == []
    assert slept == [10]


def test_a_schedule_listing_permission_error_still_fails(monkeypatch):
    def aws(region, service, action, *args):
        if action == "list-schedule-groups":
            return {"ScheduleGroups": [{"Name": "pdt", "Arn": "arn:group/pdt"}]}
        raise inventory.InventoryError("An error occurred (AccessDeniedException)")

    monkeypatch.setattr(inventory, "aws", aws)
    with pytest.raises(inventory.InventoryError, match="AccessDeniedException"):
        inventory.aws_schedules("us-east-1")


def test_batch_listings_keep_pdt_resources_and_skip_a_deleted_queue(monkeypatch):
    def aws(region, service, action, *args):
        if action == "describe-compute-environments":
            return {"computeEnvironments": [
                {"computeEnvironmentName": "pdt", "computeEnvironmentArn": "arn:ce/pdt",
                 "status": "VALID", "tags": {"managed-by": "pdt"}}]}
        if action == "describe-job-queues":
            return {"jobQueues": [{"jobQueueName": "pdt", "jobQueueArn": "arn:queue/pdt",
                                   "status": "DELETED", "tags": {"managed-by": "pdt"}}]}
        if action == "describe-job-definitions":
            return {"jobDefinitions": [
                {"jobDefinitionName": "pdt-a", "jobDefinitionArn": "arn:jd/pdt-a:1",
                 "tags": {"managed-by": "pdt"}},
                {"jobDefinitionName": "other", "jobDefinitionArn": "arn:jd/other:1"}]}
        raise AssertionError(action)

    monkeypatch.setattr(inventory, "aws", aws)
    found = (inventory.aws_compute_environments("us-east-1")
             + inventory.aws_job_queues("us-east-1")
             + inventory.aws_job_definitions("us-east-1"))
    assert [(item.kind, item.name) for item in found] == [
        ("batch compute environment", "pdt"), ("batch job definition", "pdt-a")]
    assert all(item.tags == {"managed-by": "pdt"} for item in found)


def test_the_verify_role_policy_allows_every_inventory_call():
    import aws_role

    allowed = {action for statement in aws_role.policy("123456789012")["Statement"]
               for action in statement["Action"]}
    assert {"batch:DescribeComputeEnvironments", "tag:GetResources",
            "scheduler:ListTagsForResource"} <= allowed


def test_the_listers_and_the_tag_calls_of_one_inventory_run_at_the_same_time(monkeypatch):
    all_tag_calls = threading.Barrier(3, timeout=5)

    def aws(region, service, action, *args):
        if action == "list-functions":
            return {"Functions": [
                {"FunctionName": f"pdt-{name}", "FunctionArn": f"arn:aws:lambda:x:1:pdt-{name}"}
                for name in ("b", "a")]}
        if action == "list-tags":
            all_tag_calls.wait()
            return {"Tags": {"managed-by": "pdt"}}
        if action == "list-roles":
            return {"Roles": [{"RoleName": "pdt-runner", "Arn": "arn:aws:iam::1:role/pdt-runner"}]}
        if action == "list-role-tags":
            all_tag_calls.wait()
            return {"Tags": [{"Key": "managed-by", "Value": "pdt"}]}
        return {}

    monkeypatch.setattr(inventory, "aws", aws)
    monkeypatch.setattr(inventory, "AWS_SOURCES", (inventory.aws_functions, inventory.aws_roles))
    found = inventory.aws_inventory({"region": "us-east-1"})
    assert [resource.name for resource in found] == ["pdt-runner", "pdt-a", "pdt-b"]
    assert all(resource.tags == {"managed-by": "pdt"} for resource in found)


def test_at_most_eight_cli_processes_run_at_once_across_nested_reads(monkeypatch):
    eight_calls = threading.Barrier(8, timeout=5)
    ninth_call_could_start = []

    def run(command, **kwargs):
        eight_calls.wait()
        ninth_call_could_start.append(inventory.CALLS.acquire(blocking=False))
        eight_calls.wait()
        return subprocess.CompletedProcess(command, 0, "{}", "")

    def read(outer):
        return inventory.parallel(lambda inner: inventory.aws("us-east-1", "sts", str(inner)),
                                  range(4))

    monkeypatch.setattr(inventory.subprocess, "run", run)
    assert inventory.parallel(read, range(4)) == [[{}] * 4] * 4
    assert ninth_call_could_start == [False] * 16
