import pytest
from botocore.exceptions import ClientError

from conftest import plain
from pdt import deploy_aws_batch as batch_deploy
from pdt.deploy_aws_batch import COMPUTE_ENVIRONMENT, JOB_QUEUE, LEGACY_CLUSTER, REPOSITORY


def client_error(code: str, message: str = "") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": message}}, "Delete")


def batch_missing(resource) -> ClientError:
    return client_error("ClientException", f"{resource.label} {resource.name} does not exist")


class Fake:
    def __init__(self, replies: dict, raises: dict | None = None):
        self.replies = replies
        self.raises = raises or {}

    def __getattr__(self, operation):
        def call(**kwargs):
            if operation in self.raises:
                raise self.raises[operation]
            return self.replies.get(operation, {})
        return call


class FakeBatch(Fake):
    """The shared queue and environment, listed until deleted."""

    def __init__(self, raises: dict | None = None):
        super().__init__({}, raises)
        self.definitions = []
        self.items = {resource: {"status": "VALID", "state": "ENABLED",
                                 "tags": {"managed-by": "pdt"}}
                      for resource in (JOB_QUEUE, COMPUTE_ENVIRONMENT)}

    def describe_job_definitions(self, status, jobDefinitionName=None):
        return {"jobDefinitions": [item for item in self.definitions
                                   if jobDefinitionName in (None, item["jobDefinitionName"])]}

    def __getattr__(self, operation):
        resource = next((item for item in self.items
                         if operation in (item.describe, item.update, item.delete)), None)
        if resource is None:
            return super().__getattr__(operation)

        def call(**kwargs):
            if operation in self.raises:
                raise self.raises[operation]
            if operation == resource.describe:
                present = self.items[resource]
                return {resource.key: [present] if present else []}
            if operation == resource.update:
                self.items[resource]["state"] = kwargs["state"]
            else:
                self.items[resource] = None
            return {}
        return call


class FakeEc2(Fake):
    """The default VPC holding pdt's security group; each delete takes the next reply."""

    def __init__(self, batch: FakeBatch, deletes=()):
        super().__init__({"describe_vpcs": {"Vpcs": [{"VpcId": "vpc-1"}]}})
        self.batch = batch
        self.deletes = list(deletes)
        self.group = {"GroupId": "sg-pdt", "Tags": [{"Key": "managed-by", "Value": "pdt"}]}
        self.environment_at_delete = "not deleted"

    def describe_security_groups(self, Filters):
        return {"SecurityGroups": [self.group] if self.group else []}

    def delete_security_group(self, GroupId):
        self.environment_at_delete = self.batch.items[COMPUTE_ENVIRONMENT]
        if self.deletes:
            raise self.deletes.pop(0)
        self.group = None


def fake_clients(batch_raises=None, ecs_raises=None, ecr_raises=None,
                 scheduler_raises=None, group_deletes=()) -> dict:
    missing = client_error("ResourceNotFoundException")
    batch = FakeBatch(batch_raises)
    return {
        "sts": Fake({}),
        "s3": Fake({}),
        "batch": batch,
        "ec2": FakeEc2(batch, group_deletes),
        "scheduler": Fake({"list_schedules": {"Schedules": []}},
                          {"get_schedule": missing, **(scheduler_raises or {})}),
        "secretsmanager": Fake({}, {"describe_secret": missing}),
        "logs": Fake({"describe_log_groups": {"logGroups": []}}),
        "iam": Fake({}, {"get_role": client_error("NoSuchEntity")}),
        "ecs": Fake({"describe_clusters": {"clusters": [{"status": "ACTIVE"}]}}, ecs_raises),
        "ecr": Fake({}, ecr_raises),
    }


@pytest.fixture
def destroy(monkeypatch):
    notes = []
    monkeypatch.setattr(batch_deploy, "BATCH_WAIT_DELAYS", (0, 0))
    monkeypatch.setattr(batch_deploy.console, "note", lambda message: notes.extend(plain([message])))
    monkeypatch.setattr(batch_deploy.console, "done", lambda message: None)
    monkeypatch.setattr(batch_deploy, "ensure_session", lambda app: None)
    monkeypatch.setattr(batch_deploy, "aws_settings",
                        lambda app, session: ("123456789012", "us-east-1"))
    monkeypatch.setattr(batch_deploy, "preflight", lambda *args: ("123456789012", {}))
    monkeypatch.setattr(batch_deploy, "confirm", lambda actions, assume_yes: True)

    def run(clients):
        monkeypatch.setattr(batch_deploy, "batch_clients", lambda session: clients)
        return batch_deploy.destroy({"name": "my-app", "storage": False}, assume_yes=True), notes
    return run


def test_every_shared_resource_present_is_deleted_without_a_note(destroy):
    clients = fake_clients()
    code, notes = destroy(clients)
    assert code == 0
    assert notes == []
    assert clients["batch"].items == {JOB_QUEUE: None, COMPUTE_ENVIRONMENT: None}
    assert clients["ec2"].group is None
    assert clients["ec2"].environment_at_delete is None


def test_another_app_keeps_the_shared_batch_resources_and_the_security_group(destroy):
    clients = fake_clients()
    clients["batch"].definitions = [
        {"jobDefinitionName": "pdt-other", "revision": 1, "tags": {"managed-by": "pdt"}}]
    code, _notes = destroy(clients)
    assert code == 0
    assert None not in clients["batch"].items.values()
    assert clients["ec2"].group is not None


def test_destroy_leaves_a_same_named_group_pdt_did_not_create(destroy):
    clients = fake_clients()
    clients["ec2"].group["Tags"] = []
    code, _notes = destroy(clients)
    assert code == 0
    assert clients["ec2"].environment_at_delete == "not deleted"


def test_the_security_group_is_deleted_once_the_last_job_releases_it(destroy):
    clients = fake_clients(group_deletes=[client_error("DependencyViolation")])
    code, notes = destroy(clients)
    assert code == 0
    assert notes == []
    assert clients["ec2"].group is None


def test_a_security_group_a_sibling_destroy_already_removed_counts_as_deleted(destroy):
    code, notes = destroy(fake_clients(group_deletes=[client_error("InvalidGroup.NotFound")]))
    assert code == 0
    assert f"security group {batch_deploy.SECURITY_GROUP} was already gone" in notes


def test_a_compute_environment_a_sibling_destroy_already_removed_counts_as_deleted(destroy):
    code, notes = destroy(fake_clients(
        batch_raises={COMPUTE_ENVIRONMENT.update: batch_missing(COMPUTE_ENVIRONMENT)}))
    assert code == 0
    assert f"Batch compute environment {COMPUTE_ENVIRONMENT.name} was already gone" in notes


def test_a_job_queue_a_sibling_destroy_already_removed_counts_as_deleted(destroy):
    code, notes = destroy(fake_clients(batch_raises={JOB_QUEUE.delete: batch_missing(JOB_QUEUE)}))
    assert code == 0
    assert f"Batch job queue {JOB_QUEUE.name} was already gone" in notes


def test_a_repository_a_sibling_destroy_already_removed_counts_as_deleted(destroy):
    code, notes = destroy(fake_clients(
        ecr_raises={"delete_repository": client_error("RepositoryNotFoundException")}))
    assert code == 0
    assert f"ECR repository {REPOSITORY} was already gone" in notes


def test_a_legacy_cluster_a_sibling_destroy_already_removed_counts_as_deleted(destroy):
    code, notes = destroy(fake_clients(
        ecs_raises={"delete_cluster": client_error("ClusterNotFoundException")}))
    assert code == 0
    assert f"ECS cluster {LEGACY_CLUSTER} was already gone" in notes


def test_a_schedule_group_a_sibling_destroy_already_removed_counts_as_deleted(destroy):
    code, notes = destroy(fake_clients(
        scheduler_raises={"delete_schedule_group": client_error("ResourceNotFoundException")}))
    assert code == 0
    assert f"schedule group {batch_deploy.SCHEDULE_GROUP} was already gone" in notes


@pytest.mark.parametrize("raises", [
    {"batch_raises": {JOB_QUEUE.delete: client_error("ClientException", "queue has jobs")}},
    {"batch_raises": {COMPUTE_ENVIRONMENT.update: client_error("ServerException")}},
    {"ecs_raises": {"delete_cluster": client_error("ClusterContainsTasksException")}},
    {"ecr_raises": {"delete_repository": client_error("RepositoryNotEmptyException")}},
    {"group_deletes": [client_error("DependencyViolation")] * 3},
])
def test_any_other_shared_delete_error_still_raises(destroy, raises):
    with pytest.raises(ClientError):
        destroy(fake_clients(**raises))
