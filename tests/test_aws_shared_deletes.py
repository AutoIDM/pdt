import pytest
from botocore.exceptions import ClientError

from pdt import deploy_aws_fargate as fargate


def client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": ""}}, "Delete")


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


def fake_clients(ecs_raises=None, ecr_raises=None, scheduler_raises=None) -> dict:
    missing = client_error("ResourceNotFoundException")
    return {
        "sts": Fake({}),
        "s3": Fake({}),
        "scheduler": Fake({"list_schedules": {"Schedules": []}},
                          {"get_schedule": missing, **(scheduler_raises or {})}),
        "secretsmanager": Fake({}, {"describe_secret": missing}),
        "logs": Fake({"describe_log_groups": {"logGroups": []}}),
        "iam": Fake({}, {"get_role": client_error("NoSuchEntity")}),
        "ecs": Fake({"describe_clusters": {"clusters": [{"status": "ACTIVE"}]}},
                    ecs_raises),
        "ecr": Fake({}, ecr_raises),
    }


@pytest.fixture
def destroy(monkeypatch):
    notes = []
    monkeypatch.setattr(fargate.console, "note", notes.append)
    monkeypatch.setattr(fargate.console, "done", lambda message: None)
    monkeypatch.setattr(fargate, "ensure_session", lambda app: None)
    monkeypatch.setattr(fargate, "aws_settings", lambda app, session: ("123456789012", "us-east-1"))
    monkeypatch.setattr(fargate, "preflight", lambda *args: ("123456789012", {}))
    monkeypatch.setattr(fargate, "confirm", lambda actions, assume_yes: True)

    def run(clients):
        monkeypatch.setattr(fargate, "fargate_clients", lambda session: clients)
        return fargate.destroy({"name": "my-app", "storage": False}, assume_yes=True), notes
    return run


def test_a_cluster_a_sibling_destroy_already_removed_counts_as_deleted(destroy):
    code, notes = destroy(fake_clients(
        ecs_raises={"delete_cluster": client_error("ClusterNotFoundException")}))
    assert code == 0
    assert f"ECS cluster {fargate.CLUSTER} was already gone" in notes


def test_a_repository_a_sibling_destroy_already_removed_counts_as_deleted(destroy):
    code, notes = destroy(fake_clients(
        ecr_raises={"delete_repository": client_error("RepositoryNotFoundException")}))
    assert code == 0
    assert f"ECR repository {fargate.REPOSITORY} was already gone" in notes


def test_a_schedule_group_a_sibling_destroy_already_removed_counts_as_deleted(destroy):
    code, notes = destroy(fake_clients(
        scheduler_raises={"delete_schedule_group": client_error("ResourceNotFoundException")}))
    assert code == 0
    assert f"schedule group {fargate.SCHEDULE_GROUP} was already gone" in notes


def test_any_other_shared_delete_error_still_raises(destroy):
    with pytest.raises(ClientError):
        destroy(fake_clients(
            ecs_raises={"delete_cluster": client_error("ClusterContainsTasksException")}))
