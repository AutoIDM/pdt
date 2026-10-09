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
    assert {"batch:DescribeComputeEnvironments", "tag:GetResources"} <= allowed
    assert set(aws_role.inventory_actions()) <= allowed
