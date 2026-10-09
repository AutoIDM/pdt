import pytest

from conftest import plain
from pdt import deploy_aws_batch as batch_deploy
from pdt.deploy_aws_batch import (
    COMPUTE_ENVIRONMENT, SECURITY_GROUP, ensure_compute_environment, ensure_security_group,
    find_security_group,
)

MANAGED_TAGS = [{"Key": "managed-by", "Value": "pdt"}]


class FakeEc2:
    def __init__(self, groups=()):
        self.groups = list(groups)
        self.created = []

    def describe_vpcs(self, Filters):
        return {"Vpcs": [{"VpcId": "vpc-1"}]}

    def describe_subnets(self, Filters):
        return {"Subnets": [{"SubnetId": "subnet-b"}, {"SubnetId": "subnet-a"}]}

    def describe_security_groups(self, Filters):
        return {"SecurityGroups": self.groups}

    def create_security_group(self, **kwargs):
        self.created.append(kwargs)
        return {"GroupId": "sg-new"}


class FakeBatch:
    """The shared compute environment; an update settles at once."""

    def __init__(self, groups=None):
        self.environment = None if groups is None else self.item(groups)
        self.calls = []

    def item(self, groups):
        return {"computeEnvironmentName": "pdt", "computeEnvironmentArn": "arn:ce/pdt",
                "status": "VALID", "state": "ENABLED", "tags": {"managed-by": "pdt"},
                "computeResources": {"type": "FARGATE", "securityGroupIds": groups}}

    def describe_compute_environments(self, computeEnvironments):
        return {"computeEnvironments": [self.environment] if self.environment else []}

    def create_compute_environment(self, **kwargs):
        self.calls.append(("create", kwargs["computeResources"]["securityGroupIds"]))
        self.environment = self.item(kwargs["computeResources"]["securityGroupIds"])

    def update_compute_environment(self, computeEnvironment, computeResources):
        self.calls.append(("update", computeResources["securityGroupIds"]))
        self.environment = self.item(computeResources["securityGroupIds"])


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    monkeypatch.setattr(batch_deploy, "BATCH_WAIT_DELAYS", (0, 0))


def test_a_missing_group_is_created_tagged_with_no_inbound_rules():
    ec2 = FakeEc2()
    assert find_security_group(ec2, "vpc-1") is None
    assert ensure_security_group(ec2, "vpc-1", None) == "sg-new"
    [created] = ec2.created
    assert created["GroupName"] == SECURITY_GROUP
    assert created["VpcId"] == "vpc-1"
    assert created["TagSpecifications"] == [{"ResourceType": "security-group", "Tags": [
        {"Key": "managed-by", "Value": "pdt"}, {"Key": "shared", "Value": "true"}]}]


def test_an_existing_pdt_group_is_used_as_it_is():
    ec2 = FakeEc2([{"GroupId": "sg-pdt", "Tags": MANAGED_TAGS}])
    group = find_security_group(ec2, "vpc-1")
    assert ensure_security_group(ec2, "vpc-1", group) == "sg-pdt"
    assert ec2.created == []


def test_a_same_named_group_pdt_did_not_create_stops_the_deploy():
    ec2 = FakeEc2([{"GroupId": "sg-theirs", "Tags": []}])
    with pytest.raises(SystemExit):
        ensure_security_group(ec2, "vpc-1", find_security_group(ec2, "vpc-1"))


def test_a_new_compute_environment_starts_on_the_pdt_group():
    batch = FakeBatch()
    assert ensure_compute_environment(batch, ["subnet-a"], "sg-pdt", move=False) == "arn:ce/pdt"
    assert batch.calls == [("create", ["sg-pdt"])]


def test_an_environment_on_the_default_group_moves_to_the_pdt_group_in_place():
    batch = FakeBatch(["sg-default"])
    assert ensure_compute_environment(batch, ["subnet-a"], "sg-pdt", move=True) == "arn:ce/pdt"
    assert batch.calls == [("update", ["sg-pdt"])]




@pytest.fixture
def plan(monkeypatch):
    """The deploy's plan lines; the deploy stops at the question."""
    shown = []

    def confirm(actions, assume_yes, estimate):
        shown.extend(plain(actions))
        return False

    for name, value in {
        "docker_preflight": lambda provider, assume_yes: None,
        "ensure_session": lambda app: None,
        "aws_settings": lambda app, session: ("123456789012", "us-east-1"),
        "preflight": lambda *args: ("123456789012", {}),
        "gather_secrets": lambda app: {},
        "cost_estimate_for": lambda *args: None,
        "resource_exists": lambda *args, **kwargs: False,
        "confirm": confirm,
    }.items():
        monkeypatch.setattr(batch_deploy, name, value)
    monkeypatch.setattr(batch_deploy.console, "warn", lambda message: None)

    def run(ec2, batch):
        clients = {"ec2": ec2, "batch": batch, "logs": None, "scheduler": None,
                   "secretsmanager": None, "sts": None, "iam": None}
        monkeypatch.setattr(batch_deploy, "batch_clients", lambda session: clients)
        app = {"name": "my-app", "storage": False, "schedule": "0 6 * * *",
               "timezone": "UTC", "dir": ".", "pause": False}
        assert batch_deploy.deploy(app, assume_yes=False) == 1
        return [line for line in shown if "security group" in line]
    return run


def test_the_plan_names_a_new_group_for_a_first_deploy(plan):
    assert plan(FakeEc2(), FakeBatch()) == [
        f"create security group {SECURITY_GROUP} (no inbound rules, all outbound) "
        "in default VPC vpc-1"]


def test_the_plan_names_the_move_off_the_default_group(plan):
    assert plan(FakeEc2(), FakeBatch(["sg-default"])) == [
        f"create security group {SECURITY_GROUP} (no inbound rules, all outbound) "
        "in default VPC vpc-1",
        f"move Batch compute environment {COMPUTE_ENVIRONMENT.name} from "
        f"security group sg-default to {SECURITY_GROUP}"]


def test_the_plan_reuses_the_group_an_environment_already_has(plan):
    ec2 = FakeEc2([{"GroupId": "sg-pdt", "Tags": MANAGED_TAGS}])
    assert plan(ec2, FakeBatch(["sg-pdt"])) == [
        f"use security group {SECURITY_GROUP} (sg-pdt) in default VPC vpc-1"]
