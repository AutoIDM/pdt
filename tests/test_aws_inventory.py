import pytest

import inventory
from verify import empty_check, wait_for


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
    problems = wait_for(lambda: inventory.aws_schedules("us-east-1"), empty_check,
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
