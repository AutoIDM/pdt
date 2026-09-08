import json
import sys
import types
from unittest.mock import Mock

import pytest

if "botocore.exceptions" not in sys.modules:
    sys.modules.setdefault("boto3", types.ModuleType("boto3"))
    exceptions = types.ModuleType("botocore.exceptions")
    exceptions.ClientError = type("ClientError", (Exception,), {})
    botocore = types.ModuleType("botocore")
    botocore.exceptions = exceptions
    sys.modules.setdefault("botocore", botocore)
    sys.modules["botocore.exceptions"] = exceptions

from pdt import deploy_aws_fargate, terraform_aws
from pdt.deploy_aws import not_found, other_schedules
from pdt.deploy_common import CostEstimate


class MissingResource(Exception):
    response = {"Error": {"Code": "ResourceNotFoundException"}}


@pytest.fixture
def app(tmp_path):
    return {"name": "report", "dir": tmp_path, "schedule": "0 9 * * 1-5",
            "timezone": "America/New_York", "platform": {"region": "us-east-1"},
            "storage": False}


@pytest.fixture
def clients():
    values = {name: Mock() for name in
              ("secretsmanager", "logs", "iam", "scheduler", "lambda", "ecs", "ecr")}
    values["secretsmanager"].describe_secret.side_effect = MissingResource()
    values["logs"].describe_log_groups.return_value = {"logGroups": []}
    values["iam"].get_role.side_effect = MissingResource()
    values["scheduler"].get_schedule.side_effect = MissingResource()
    values["scheduler"].get_schedule_group.side_effect = MissingResource()
    values["lambda"].get_function.side_effect = MissingResource()
    values["ecs"].describe_task_definition.side_effect = MissingResource()
    values["ecs"].describe_clusters.return_value = {"clusters": []}
    values["ecr"].describe_repositories.side_effect = MissingResource()
    return values


def test_fargate_pins_image_digest_and_connects_scheduler_to_task(app):
    names = deploy_aws_fargate.resource_names(app["name"])
    image = "012345678901.dkr.ecr.us-east-1.amazonaws.com/pdt@sha256:abcd"
    document = terraform_aws.fargate_configuration(
        app, names, "012345678901", "us-east-1", image, ["subnet-a"], "sg-a")
    resources = document["resource"]
    schedule = resources["aws_scheduler_schedule"]["app"]
    assert schedule["schedule_expression"] == "cron(0 9 ? * MON-FRI *)"
    assert schedule["schedule_expression_timezone"] == "America/New_York"
    assert "aws_secretsmanager_secret_version" not in resources
    assert resources["aws_secretsmanager_secret"]["env"]["recovery_window_in_days"] == 0
    task = resources["aws_ecs_task_definition"]["app"]
    container = json.loads(task["container_definitions"])[0]
    assert container["image"] == image
    assert container["secrets"] == [{"name": "PDT_ENV_JSON", "valueFrom": "${aws_secretsmanager_secret.env.arn}"}]
    assert task["runtime_platform"]["cpu_architecture"] == "ARM64"
    parameters = schedule["target"]["ecs_parameters"]
    assert parameters["task_definition_arn"] == "${aws_ecs_task_definition.app.arn}"
    assert parameters["network_configuration"] == {
        "subnets": ["subnet-a"], "security_groups": ["sg-a"], "assign_public_ip": True}
    assert "aws_ecr_repository" not in resources
    assert "aws_ecs_cluster" not in resources


def test_shared_definitions_keep_resources_out_of_app_state():
    document = terraform_aws.shared_configuration("us-east-1", fargate=True)
    resources = document["resource"]
    assert set(resources) == {"aws_scheduler_schedule_group", "aws_ecr_repository",
                              "aws_ecs_cluster", "aws_ecs_cluster_capacity_providers"}
    assert resources["aws_ecr_repository"]["pdt"]["force_delete"] is True
    for kind in ("aws_scheduler_schedule_group", "aws_ecr_repository", "aws_ecs_cluster"):
        assert resources[kind]["pdt"]["tags"]["managed-by"] == "pdt"


def test_new_apps_import_nothing(clients):
    assert terraform_aws.app_imports(clients, deploy_aws_fargate.resource_names("report"), fargate=True) == {}
    assert terraform_aws.shared_imports(clients, fargate=True) == {}


def test_import_refuses_untagged_secret(clients):
    clients["secretsmanager"].describe_secret.side_effect = None
    clients["secretsmanager"].describe_secret.return_value = {"ARN": "secret", "Tags": []}
    with pytest.raises(SystemExit):
        terraform_aws.app_imports(clients, deploy_aws_fargate.resource_names("report"), fargate=True)


def test_import_refuses_untagged_role(clients):
    clients["iam"].get_role.side_effect = None
    clients["iam"].get_role.return_value = {"Role": {"Tags": []}}
    with pytest.raises(SystemExit):
        terraform_aws.app_imports(clients, deploy_aws_fargate.resource_names("report"), fargate=True)


def test_schedule_import_checks_its_app_role(clients):
    clients["scheduler"].get_schedule.side_effect = None
    clients["scheduler"].get_schedule.return_value = {
        "Description": "Managed by PDT", "Target": {"RoleArn": "arn:aws:iam::123:role/unrelated"}}
    with pytest.raises(SystemExit):
        terraform_aws.app_imports(clients, deploy_aws_fargate.resource_names("report"), fargate=True)


def test_shared_import_refuses_untagged_group(clients):
    clients["scheduler"].get_schedule_group.side_effect = None
    clients["scheduler"].get_schedule_group.return_value = {"Arn": "group"}
    clients["scheduler"].list_tags_for_resource.return_value = {"Tags": []}
    with pytest.raises(SystemExit):
        terraform_aws.shared_imports(clients)


def test_shared_import_refuses_untagged_registry(clients):
    clients["ecr"].describe_repositories.side_effect = None
    clients["ecr"].describe_repositories.return_value = {"repositories": [{"repositoryArn": "repo"}]}
    clients["ecr"].list_tags_for_resource.return_value = {"tags": []}
    with pytest.raises(SystemExit):
        terraform_aws.shared_imports(clients, fargate=True)


def test_shared_import_refuses_untagged_cluster(clients):
    clients["ecs"].describe_clusters.return_value = {"clusters": [{"status": "ACTIVE", "tags": []}]}
    with pytest.raises(SystemExit):
        terraform_aws.shared_imports(clients, fargate=True)


def test_import_preserves_native_resource_identifiers(clients):
    names = deploy_aws_fargate.resource_names("report")
    clients["secretsmanager"].describe_secret.side_effect = None
    clients["secretsmanager"].describe_secret.return_value = {
        "ARN": "arn:secret", "Tags": [{"Key": "managed-by", "Value": "pdt"}]}
    clients["iam"].get_role.side_effect = None
    clients["iam"].get_role.return_value = {"Role": {"Tags": [{"Key": "managed-by", "Value": "pdt"}]}}
    clients["iam"].list_role_policies.return_value = {"PolicyNames": ["pdt-execution", "pdt-scheduler"]}
    clients["scheduler"].get_schedule.side_effect = None
    clients["scheduler"].get_schedule.return_value = {
        "Description": "Managed by PDT", "Target": {"RoleArn": "arn:aws:iam::123:role/pdt-report-scheduler"}}
    imports = terraform_aws.app_imports(clients, names, fargate=True)
    assert imports["aws_secretsmanager_secret.env"] == "arn:secret"
    assert imports["aws_iam_role.execution"] == "pdt-report-execution"
    assert imports["aws_iam_role_policy.execution"] == "pdt-report-execution:pdt-execution"
    assert imports["aws_scheduler_schedule.app"] == "pdt/pdt-report"


def test_secret_value_updates_do_not_create_infrastructure():
    secrets = Mock()
    secrets.get_secret_value.side_effect = MissingResource()
    terraform_aws.write_secret_value(secrets, "arn:secret", '{"TOKEN": "value"}')
    secrets.put_secret_value.assert_called_once_with(SecretId="arn:secret", SecretString='{"TOKEN": "value"}')
    secrets.create_secret.assert_not_called()
    secrets.get_secret_value.side_effect = None
    secrets.get_secret_value.return_value = {"SecretString": '{"TOKEN": "value"}'}
    terraform_aws.write_secret_value(secrets, "arn:secret", '{"TOKEN": "value"}')
    assert secrets.put_secret_value.call_count == 1


def test_missing_ecs_family_is_not_a_deploy_failure():
    missing = Exception()
    missing.response = {"Error": {"Code": "ClientException", "Message": "Unable to describe task definition."}}
    assert not_found(missing)
    missing.response["Error"]["Message"] = "Access denied"
    assert not not_found(missing)


def test_shared_schedule_cleanup_checks_later_pages():
    scheduler = Mock()
    scheduler.list_schedules.side_effect = [
        {"Schedules": [{"Name": "pdt-report"}], "NextToken": "next"},
        {"Schedules": [{"Name": "pdt-other"}]},
    ]
    assert other_schedules(scheduler, "pdt-report") == ["pdt-other"]
    scheduler.list_schedules.assert_called_with(GroupName="pdt", NextToken="next")


def test_shared_registry_cleanup_checks_later_pages():
    ecr = Mock()
    ecr.get_paginator.return_value.paginate.return_value = [
        {"imageIds": [{"imageTag": "report-latest"}]},
        {"imageIds": [{"imageTag": "other-latest"}]},
    ]
    assert not deploy_aws_fargate.repository_unused_after(ecr, "report-latest")


def test_shared_cluster_cleanup_checks_later_pages():
    ecs = Mock()
    ecs.describe_clusters.return_value = {"clusters": [{"status": "ACTIVE"}]}
    ecs.list_tasks.return_value = {"taskArns": []}
    ecs.get_paginator.return_value.paginate.return_value = [
        {"families": ["pdt-report"]}, {"families": ["pdt-other"]},
    ]
    assert not deploy_aws_fargate.cluster_unused_after(ecs, "pdt-report")


def test_deploy_creates_prerequisites_before_build_and_secret_value(app, monkeypatch):
    module = deploy_aws_fargate
    events = []
    session = Mock()
    cloud = {name: Mock() for name in ("sts", "iam", "logs", "secretsmanager", "lambda", "scheduler", "ecr", "ec2")}
    monkeypatch.setattr(module, "ensure_session", lambda *_: session)
    monkeypatch.setattr(module, "aws_settings", lambda *_: ("012345678901", "us-east-1"))
    monkeypatch.setattr(module, "preflight", lambda *_: ("012345678901", "identity"))
    monkeypatch.setattr(module, "resource_exists", lambda *_a, **_k: False)
    monkeypatch.setattr(module, "gather_secrets", lambda *_: {"TOKEN": "private-value"})
    monkeypatch.setattr(module, "environment", lambda *_: {})
    monkeypatch.setattr(module, "shared_imports", lambda *_a, **_k: {})
    monkeypatch.setattr(module, "app_imports", lambda *_a, **_k: {})
    monkeypatch.setattr(module, "cost_estimate_for", lambda *_: CostEstimate([("cost", 0.0)], "test prices"))
    monkeypatch.setattr(module, "confirm", lambda *_: events.append("confirm") or True)
    monkeypatch.setattr(module, "write_secret_value", lambda *_: events.append("secret-value"))

    class Workspace:
        def __init__(self, name):
            self.name = name

        def plan(self):
            return self.name

        def apply(self, plan):
            events.append(plan + "-apply")

        def outputs(self):
            return {"secret_arn": "arn:secret"}

        def reconcile(self, document, imports):
            assert "private-value" not in json.dumps(document)
            events.append("runtime-apply")

    class Deployment:
        def __init__(self, *_args):
            self.state_dir = app["dir"]

        def __enter__(self):
            events.append("backend")
            return self

        def __exit__(self, *_args):
            pass

        def save(self):
            events.append("save")

        def workspace(self, name, document, imports, retain=False):
            assert retain
            assert "private-value" not in json.dumps(document)
            return Workspace(name)

    monkeypatch.setattr(module, "Deployment", Deployment)
    monkeypatch.setattr(module, "fargate_clients", lambda *_: cloud)
    monkeypatch.setattr(module, "docker_preflight", lambda: None)
    monkeypatch.setattr(module, "default_network", lambda *_: (["subnet-a"], "sg-a"))
    monkeypatch.setattr(module, "build_and_push", lambda *_: events.append("build") or "sha256:abcd")
    assert module.deploy(app, True) == 0
    assert events == ["confirm", "backend", "save", "shared-apply", "build", "app-apply", "secret-value", "runtime-apply", "save"]
