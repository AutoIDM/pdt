"""Native AWS infrastructure definitions and adoption of existing PDT resources."""

from __future__ import annotations

import json
from pathlib import Path

from pdt import config
from pdt.deploy_aws import (
    MANAGED_TAGS, SCHEDULE_GROUP, aws_schedule_expression, has_managed_tag,
    not_found, trust_policy,
)
from pdt.deploy_common import fail
from pdt.terraform import configuration


def environment(session, region: str) -> dict[str, str]:
    credentials = session.get_credentials().get_frozen_credentials()
    result = {
        "AWS_ACCESS_KEY_ID": credentials.access_key,
        "AWS_SECRET_ACCESS_KEY": credentials.secret_key,
        "AWS_REGION": region,
        "AWS_DEFAULT_REGION": region,
    }
    if credentials.token:
        result["AWS_SESSION_TOKEN"] = credentials.token
    return result


def shared_configuration(region: str, fargate: bool = False) -> dict:
    resources = {
        "aws_scheduler_schedule_group": {
            "pdt": {"name": SCHEDULE_GROUP, "tags": dict(MANAGED_TAGS)},
        },
    }
    if fargate:
        resources["aws_ecr_repository"] = {"pdt": {
            "name": "pdt",
            "force_delete": True,
            "image_scanning_configuration": {"scan_on_push": True},
            "encryption_configuration": {"encryption_type": "AES256"},
            "tags": {**MANAGED_TAGS, "shared": "true"},
        }}
        resources["aws_ecs_cluster"] = {"pdt": {
            "name": "pdt", "tags": {**MANAGED_TAGS, "shared": "true"},
        }}
        resources["aws_ecs_cluster_capacity_providers"] = {"pdt": {
            "cluster_name": "${aws_ecs_cluster.pdt.name}",
            "capacity_providers": ["FARGATE"],
        }}
    return configuration("aws", {"region": region}, resources)


def base_configuration(names: dict[str, str], region: str) -> dict:
    return configuration("aws", {"region": region}, {
        "aws_secretsmanager_secret": {"env": {
            "name": names["secret"],
            "description": "PDT_ENV_JSON for a PDT app",
            "recovery_window_in_days": 0,
            "tags": dict(MANAGED_TAGS),
        }},
        "aws_cloudwatch_log_group": {"app": {
            "name": names["log_group"],
            "retention_in_days": 30,
            "tags": dict(MANAGED_TAGS),
        }},
    }, outputs={"secret_arn": {"value": "${aws_secretsmanager_secret.env.arn}"}})


def lambda_configuration(app: dict, names: dict[str, str], account: str,
                         region: str, archive: Path, digest: str | None) -> dict:
    document = base_configuration(names, region)
    resources = document["resource"]
    resources["aws_iam_role"] = {
        "function": {
            "name": names["function_role"],
            "assume_role_policy": trust_policy("lambda.amazonaws.com"),
            "description": "Managed by PDT", "tags": dict(MANAGED_TAGS),
        },
        "scheduler": {
            "name": names["scheduler_role"],
            "assume_role_policy": trust_policy("scheduler.amazonaws.com"),
            "description": "Managed by PDT", "tags": dict(MANAGED_TAGS),
        },
    }
    resources["aws_iam_role_policy"] = {
        "function": {
            "name": "pdt-function", "role": "${aws_iam_role.function.id}",
            "policy": json.dumps({"Version": "2012-10-17", "Statement": [
                {"Effect": "Allow", "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
                 "Resource": f"arn:aws:logs:{region}:{account}:log-group:{names['log_group']}:*"},
                {"Effect": "Allow", "Action": ["secretsmanager:GetSecretValue"],
                 "Resource": "${aws_secretsmanager_secret.env.arn}"},
            ]}),
        },
        "scheduler": {
            "name": "pdt-scheduler", "role": "${aws_iam_role.scheduler.id}",
            "policy": json.dumps({"Version": "2012-10-17", "Statement": [
                {"Effect": "Allow", "Action": ["lambda:InvokeFunction"],
                 "Resource": f"arn:aws:lambda:{region}:{account}:function:{names['function']}"},
            ]}),
        },
    }
    function = {
        "function_name": names["function"],
        "filename": str(archive),
        "role": "${aws_iam_role.function.arn}",
        "runtime": "python3.12", "handler": "lambda_function.lambda_handler",
        "architectures": ["arm64"], "memory_size": 512, "timeout": 900,
        "description": "Managed by PDT", "tags": dict(MANAGED_TAGS),
        "environment": {"variables": {"PDT_SECRET_ARN": "${aws_secretsmanager_secret.env.arn}"}},
        "depends_on": ["aws_cloudwatch_log_group.app", "aws_iam_role_policy.function"],
    }
    if digest is not None:
        function["source_code_hash"] = digest
    resources["aws_lambda_function"] = {"app": function}
    resources["aws_scheduler_schedule"] = {"app": {
        "name": names["schedule"], "group_name": SCHEDULE_GROUP,
        "description": "Managed by PDT", "action_after_completion": "NONE",
        "schedule_expression": aws_schedule_expression(config.cron_expression(app["schedule"])),
        "schedule_expression_timezone": app["timezone"], "state": "ENABLED",
        "flexible_time_window": {"mode": "OFF"},
        "target": {
            "arn": "${aws_lambda_function.app.arn}",
            "role_arn": "${aws_iam_role.scheduler.arn}",
            "retry_policy": {"maximum_retry_attempts": 1},
        },
        "depends_on": ["aws_iam_role_policy.scheduler"],
    }}
    return document


def fargate_configuration(app: dict, names: dict[str, str], account: str,
                          region: str, image: str, subnets: list[str],
                          security_group: str, variables: dict[str, str] | None = None,
                          grants: list[dict] | None = None) -> dict:
    document = base_configuration(names, region)
    resources = document["resource"]
    resources["aws_iam_role"] = {}
    for role, service in (("execution", "ecs-tasks.amazonaws.com"),
                          ("task", "ecs-tasks.amazonaws.com"),
                          ("scheduler", "scheduler.amazonaws.com")):
        resources["aws_iam_role"][role] = {
            "name": names[f"{role}_role"], "assume_role_policy": trust_policy(service),
            "description": "Managed by PDT", "tags": dict(MANAGED_TAGS),
        }
    resources["aws_iam_role_policy"] = {
        "execution": {
            "name": "pdt-execution", "role": "${aws_iam_role.execution.id}",
            "policy": json.dumps({"Version": "2012-10-17", "Statement": [
                {"Effect": "Allow", "Action": ["ecr:GetAuthorizationToken"], "Resource": "*"},
                {"Effect": "Allow", "Action": ["ecr:BatchCheckLayerAvailability",
                 "ecr:GetDownloadUrlForLayer", "ecr:BatchGetImage"],
                 "Resource": f"arn:aws:ecr:{region}:{account}:repository/pdt"},
                {"Effect": "Allow", "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
                 "Resource": f"arn:aws:logs:{region}:{account}:log-group:{names['log_group']}:*"},
                {"Effect": "Allow", "Action": ["secretsmanager:GetSecretValue"],
                 "Resource": "${aws_secretsmanager_secret.env.arn}"},
            ]}),
        },
        "scheduler": {
            "name": "pdt-scheduler", "role": "${aws_iam_role.scheduler.id}",
            "policy": json.dumps({"Version": "2012-10-17", "Statement": [
                {"Effect": "Allow", "Action": ["ecs:RunTask"],
                 "Resource": f"arn:aws:ecs:{region}:{account}:task-definition/{names['family']}:*"},
                {"Effect": "Allow", "Action": ["iam:PassRole"],
                 "Resource": ["${aws_iam_role.execution.arn}", "${aws_iam_role.task.arn}"]},
            ]}),
        },
    }
    if grants:
        resources["aws_iam_role_policy"]["task"] = {
            "name": "pdt-task", "role": "${aws_iam_role.task.id}",
            "policy": json.dumps({"Version": "2012-10-17", "Statement": grants}),
        }
    resources["aws_ecs_task_definition"] = {"app": {
        "family": names["family"], "execution_role_arn": "${aws_iam_role.execution.arn}",
        "task_role_arn": "${aws_iam_role.task.arn}", "network_mode": "awsvpc",
        "requires_compatibilities": ["FARGATE"], "cpu": "256", "memory": "512",
        "runtime_platform": {"cpu_architecture": "ARM64", "operating_system_family": "LINUX"},
        "container_definitions": json.dumps([{
            "name": names["family"], "image": image, "essential": True,
            "environment": [{"name": name, "value": value}
                            for name, value in (variables or {}).items()],
            "secrets": [{"name": "PDT_ENV_JSON", "valueFrom": "${aws_secretsmanager_secret.env.arn}"}],
            "logConfiguration": {"logDriver": "awslogs", "options": {
                "awslogs-group": names["log_group"], "awslogs-region": region,
                "awslogs-stream-prefix": "ecs",
            }},
        }]),
        "tags": dict(MANAGED_TAGS),
        "depends_on": ["aws_cloudwatch_log_group.app", "aws_iam_role_policy.execution"],
    }}
    resources["aws_scheduler_schedule"] = {"app": {
        "name": names["schedule"], "group_name": SCHEDULE_GROUP,
        "description": "Managed by PDT", "action_after_completion": "NONE",
        "schedule_expression": aws_schedule_expression(config.cron_expression(app["schedule"])),
        "schedule_expression_timezone": app["timezone"], "state": "ENABLED",
        "flexible_time_window": {"mode": "OFF"},
        "target": {
            "arn": f"arn:aws:ecs:{region}:{account}:cluster/pdt",
            "role_arn": "${aws_iam_role.scheduler.arn}",
            "retry_policy": {"maximum_retry_attempts": 1},
            "ecs_parameters": {
                "task_definition_arn": "${aws_ecs_task_definition.app.arn}",
                "launch_type": "FARGATE", "task_count": 1,
                "network_configuration": {"subnets": subnets,
                    "security_groups": [security_group], "assign_public_ip": True},
            },
        },
        "depends_on": ["aws_iam_role_policy.scheduler"],
    }}
    return document


def shared_imports(clients: dict, fargate: bool = False) -> dict[str, str]:
    imports = {}
    scheduler = clients["scheduler"]
    try:
        group = scheduler.get_schedule_group(Name=SCHEDULE_GROUP)
        tags = scheduler.list_tags_for_resource(ResourceArn=group["Arn"]).get("Tags", [])
        if not has_managed_tag(tags, "Key", "Value"):
            fail(f"Schedule group {SCHEDULE_GROUP} exists but is not managed by PDT")
        imports["aws_scheduler_schedule_group.pdt"] = SCHEDULE_GROUP
    except Exception as exc:
        if not not_found(exc):
            raise
    if not fargate:
        return imports
    try:
        repository = clients["ecr"].describe_repositories(repositoryNames=["pdt"])["repositories"][0]
        tags = clients["ecr"].list_tags_for_resource(resourceArn=repository["repositoryArn"]).get("tags", [])
        if not has_managed_tag(tags, "Key", "Value"):
            fail("ECR repository pdt exists but is not managed by PDT")
        imports["aws_ecr_repository.pdt"] = "pdt"
    except Exception as exc:
        if not not_found(exc):
            raise
    clusters = clients["ecs"].describe_clusters(clusters=["pdt"], include=["TAGS"]).get("clusters", [])
    for cluster in clusters:
        if cluster.get("status") != "ACTIVE":
            continue
        if not has_managed_tag(cluster.get("tags", []), "key", "value"):
            fail("ECS cluster pdt exists but is not managed by PDT")
        imports["aws_ecs_cluster.pdt"] = cluster["clusterArn"]
        imports["aws_ecs_cluster_capacity_providers.pdt"] = "pdt"
    return imports


def app_imports(clients: dict, names: dict[str, str], fargate: bool = False,
                storage: bool = False) -> dict[str, str]:
    imports = {}
    try:
        secret = clients["secretsmanager"].describe_secret(SecretId=names["secret"])
        if not has_managed_tag(secret.get("Tags", []), "Key", "Value"):
            fail(f"Secrets Manager secret {names['secret']} exists but is not managed by PDT")
        if secret.get("DeletedDate"):
            fail(f"Secrets Manager secret {names['secret']} is pending deletion; restore it before deploying")
        imports["aws_secretsmanager_secret.env"] = secret["ARN"]
    except Exception as exc:
        if not not_found(exc):
            raise
    groups = clients["logs"].describe_log_groups(logGroupNamePrefix=names["log_group"]).get("logGroups", [])
    for group in groups:
        if group["logGroupName"] != names["log_group"]:
            continue
        arn = group.get("logGroupArn") or group["arn"].removesuffix(":*")
        tags = clients["logs"].list_tags_for_resource(resourceArn=arn).get("tags", {})
        if tags.get("managed-by") != "pdt":
            fail(f"CloudWatch log group {names['log_group']} exists but is not managed by PDT")
        imports["aws_cloudwatch_log_group.app"] = names["log_group"]
    roles = ["execution", "task", "scheduler"] if fargate else ["function", "scheduler"]
    for role in roles:
        name = names[f"{role}_role"]
        try:
            current = clients["iam"].get_role(RoleName=name)["Role"]
            if not has_managed_tag(current.get("Tags", []), "Key", "Value"):
                fail(f"IAM role {name} exists but is not managed by PDT")
            imports[f"aws_iam_role.{role}"] = name
            policies = clients["iam"].list_role_policies(RoleName=name).get("PolicyNames", [])
            if (role != "task" or storage) and f"pdt-{role}" in policies:
                imports[f"aws_iam_role_policy.{role}"] = f"{name}:pdt-{role}"
        except Exception as exc:
            if not not_found(exc):
                raise
    try:
        schedule = clients["scheduler"].get_schedule(Name=names["schedule"], GroupName=SCHEDULE_GROUP)
        if (schedule.get("Description") != "Managed by PDT"
                or not schedule.get("Target", {}).get("RoleArn", "").endswith(f"/{names['scheduler_role']}")):
            fail(f"Schedule {names['schedule']} exists but is not managed by this PDT app")
        imports["aws_scheduler_schedule.app"] = f"{SCHEDULE_GROUP}/{names['schedule']}"
    except Exception as exc:
        if not not_found(exc):
            raise
    if fargate:
        try:
            task = clients["ecs"].describe_task_definition(taskDefinition=names["family"], include=["TAGS"])
            if not has_managed_tag(task.get("tags", []), "key", "value"):
                fail(f"ECS task family {names['family']} exists but is not managed by PDT")
            imports["aws_ecs_task_definition.app"] = task["taskDefinition"]["taskDefinitionArn"]
        except Exception as exc:
            if not not_found(exc):
                raise
    else:
        try:
            function = clients["lambda"].get_function(FunctionName=names["function"])
            arn = function["Configuration"]["FunctionArn"]
            tags = clients["lambda"].list_tags(Resource=arn).get("Tags", {})
            if tags.get("managed-by") != "pdt":
                fail(f"Lambda function {names['function']} exists but is not managed by PDT")
            imports["aws_lambda_function.app"] = names["function"]
        except Exception as exc:
            if not not_found(exc):
                raise
    return imports


def write_secret_value(secrets, arn: str, payload: str) -> None:
    try:
        current = secrets.get_secret_value(SecretId=arn).get("SecretString")
    except Exception as exc:
        if not not_found(exc):
            raise
        current = None
    if current != payload:
        secrets.put_secret_value(SecretId=arn, SecretString=payload)
