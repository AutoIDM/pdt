"""Deploy an app as an ECS Fargate task run by EventBridge Scheduler.

Selected with platform.runtime: fargate. Entered through deploy_aws.py,
which owns the uv script header, login, and permission handling.
"""

from __future__ import annotations

import base64
import json
import shutil
import subprocess

from pdt import config
from pdt.deploy import confirm
from pdt.deploy_aws import (
    COMMON_ACTIONS, SCHEDULE_GROUP, aws_schedule_expression,
    aws_settings, clients_for, cost_estimate_lines, delete_log_group, delete_role,
    delete_secret, list_price, recent_stream_seconds, run_basis,
    ensure_log_group, ensure_role, ensure_schedule, ensure_secret,
    ensure_session, has_managed_tag, iam_tags, not_found,
    delete_schedule_group, other_schedules, preflight, resource_exists,
    with_role_propagation_retry,
)
from pdt.deploy_common import DOCKERFILE, fail, gather_secrets, stage_build_context

CLUSTER = "pdt"
REPOSITORY = "pdt"
TASK_CPU = "256"
TASK_MEMORY = "512"
TASK_ARCHITECTURE = "ARM64"
DOCKER_PLATFORM = "linux/arm64"
FARGATE_MIN_SECONDS = 60
FARGATE_ACTIONS = [
    "ec2:DescribeSecurityGroups",
    "ec2:DescribeSubnets",
    "ec2:DescribeVpcs",
    "ecr:BatchCheckLayerAvailability",
    "ecr:BatchDeleteImage",
    "ecr:CompleteLayerUpload",
    "ecr:CreateRepository",
    "ecr:DescribeImages",
    "ecr:DeleteRepository",
    "ecr:DescribeRepositories",
    "ecr:GetAuthorizationToken",
    "ecr:InitiateLayerUpload",
    "ecr:ListImages",
    "ecr:PutImage",
    "ecr:UploadLayerPart",
    "ecs:CreateCluster",
    "ecs:DeleteCluster",
    "ecs:DeregisterTaskDefinition",
    "ecs:DescribeClusters",
    "ecs:DescribeTaskDefinition",
    "ecs:ListTagsForResource",
    "ecs:ListTaskDefinitionFamilies",
    "ecs:ListTaskDefinitions",
    "ecs:ListTasks",
    "ecs:RegisterTaskDefinition",
    "ecs:TagResource",
]
DEPLOYER_ACTIONS = sorted(COMMON_ACTIONS + FARGATE_ACTIONS)


def resource_names(app_name: str) -> dict[str, str]:
    base = f"pdt-{app_name}"
    return {
        "family": base,
        "schedule": base,
        "secret": f"{base}-env",
        "log_group": f"/pdt/{app_name}",
        "execution_role": f"{base}-execution",
        "task_role": f"{base}-task",
        "scheduler_role": f"{base}-scheduler",
        "image_tag": f"{app_name}-latest",
    }


def tags_list(extra: dict[str, str] | None = None) -> list[dict[str, str]]:
    return [{"key": tag["Key"], "value": tag["Value"]} for tag in iam_tags(extra)]


def docker_preflight() -> None:
    if not shutil.which("docker"):
        fail("Docker is required for platform.runtime: fargate; install Docker Desktop "
             "or set platform.runtime: lambda")
    proc = subprocess.run(["docker", "info"], capture_output=True, text=True, check=False)
    if proc.returncode:
        fail("Docker is installed but not running; start Docker and run the same command again")


def default_network(ec2) -> tuple[list[str], str]:
    vpcs = ec2.describe_vpcs(Filters=[{"Name": "is-default", "Values": ["true"]}])["Vpcs"]
    if not vpcs:
        fail("this account/region has no default VPC; custom VPC configuration is not supported yet")
    vpc_id = vpcs[0]["VpcId"]
    subnets = ec2.describe_subnets(Filters=[
        {"Name": "vpc-id", "Values": [vpc_id]},
        {"Name": "state", "Values": ["available"]},
        {"Name": "default-for-az", "Values": ["true"]},
    ])["Subnets"]
    subnet_ids = sorted(subnet["SubnetId"] for subnet in subnets)
    if not subnet_ids:
        fail(f"default VPC {vpc_id} has no available default subnets")
    groups = ec2.describe_security_groups(Filters=[
        {"Name": "vpc-id", "Values": [vpc_id]},
        {"Name": "group-name", "Values": ["default"]},
    ])["SecurityGroups"]
    if not groups:
        fail(f"default VPC {vpc_id} has no default security group")
    return subnet_ids, groups[0]["GroupId"]


def ensure_repository(ecr) -> str:
    try:
        repos = ecr.describe_repositories(repositoryNames=[REPOSITORY])["repositories"]
        return repos[0]["repositoryUri"]
    except Exception as exc:
        if not not_found(exc):
            raise
    repo = ecr.create_repository(
        repositoryName=REPOSITORY,
        imageScanningConfiguration={"scanOnPush": True},
        encryptionConfiguration={"encryptionType": "AES256"},
        tags=iam_tags({"shared": "true"}),
    )["repository"]
    return repo["repositoryUri"]


def ensure_cluster(ecs) -> str:
    response = ecs.describe_clusters(clusters=[CLUSTER])
    active = [item for item in response.get("clusters", []) if item.get("status") == "ACTIVE"]
    if active:
        return active[0]["clusterArn"]
    created = ecs.create_cluster(
        clusterName=CLUSTER,
        capacityProviders=["FARGATE"],
        tags=tags_list({"shared": "true"}),
    )
    return created["cluster"]["clusterArn"]


def ensure_roles(iam, names: dict[str, str], account: str, region: str,
                 secret_arn: str) -> tuple[str, str, str]:
    execution = ensure_role(
        iam, names["execution_role"], "ecs-tasks.amazonaws.com", "pdt-execution", [
            {"Effect": "Allow",
             "Action": ["ecr:GetAuthorizationToken"], "Resource": "*"},
            {"Effect": "Allow",
             "Action": ["ecr:BatchCheckLayerAvailability", "ecr:GetDownloadUrlForLayer",
                        "ecr:BatchGetImage"],
             "Resource": f"arn:aws:ecr:{region}:{account}:repository/{REPOSITORY}"},
            {"Effect": "Allow",
             "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
             "Resource": f"arn:aws:logs:{region}:{account}:log-group:{names['log_group']}:*"},
            {"Effect": "Allow", "Action": ["secretsmanager:GetSecretValue"],
             "Resource": secret_arn},
        ])
    # The task role carries no application permissions yet.
    task = ensure_role(iam, names["task_role"], "ecs-tasks.amazonaws.com", "pdt-task", [])
    task_definition = f"arn:aws:ecs:{region}:{account}:task-definition/{names['family']}:*"
    scheduler = ensure_role(
        iam, names["scheduler_role"], "scheduler.amazonaws.com", "pdt-scheduler", [
            {"Effect": "Allow", "Action": ["ecs:RunTask"], "Resource": task_definition},
            {"Effect": "Allow", "Action": ["iam:PassRole"],
             "Resource": [execution, task]},
        ])
    return execution, task, scheduler


def desired_task(names: dict[str, str], image: str, region: str,
                 execution_role: str, task_role: str, secret_arn: str) -> dict:
    return {
        "family": names["family"],
        "taskRoleArn": task_role,
        "executionRoleArn": execution_role,
        "networkMode": "awsvpc",
        "requiresCompatibilities": ["FARGATE"],
        "cpu": TASK_CPU,
        "memory": TASK_MEMORY,
        "runtimePlatform": {"cpuArchitecture": TASK_ARCHITECTURE,
                            "operatingSystemFamily": "LINUX"},
        "containerDefinitions": [{
            "name": names["family"],
            "image": image,
            "essential": True,
            "secrets": [{"name": "PDT_ENV_JSON", "valueFrom": secret_arn}],
            "logConfiguration": {
                "logDriver": "awslogs",
                "options": {
                    "awslogs-group": names["log_group"],
                    "awslogs-region": region,
                    "awslogs-stream-prefix": "ecs",
                },
            },
        }],
    }


def normalized_task(task: dict) -> dict:
    keys = ("family", "taskRoleArn", "executionRoleArn", "networkMode",
            "requiresCompatibilities", "cpu", "memory", "runtimePlatform")
    result = {key: task.get(key) for key in keys}
    container_keys = ("name", "image", "essential", "secrets", "logConfiguration")
    result["containerDefinitions"] = [
        {key: container.get(key) for key in container_keys}
        for container in task.get("containerDefinitions", [])
    ]
    return result


def ensure_task_definition(ecs, desired: dict, image_digest: str) -> str:
    try:
        current = ecs.describe_task_definition(
            taskDefinition=desired["family"], include=["TAGS"])
        definition = current["taskDefinition"]
        if not has_managed_tag(current.get("tags", []), "key", "value"):
            fail(f"ECS task family {desired['family']} exists but is not managed by PDT")
        same_image = any(tag.get("key") == "image-digest" and tag.get("value") == image_digest
                         for tag in current.get("tags", []))
        if same_image and normalized_task(definition) == normalized_task(desired):
            return definition["taskDefinitionArn"]
    except Exception as exc:
        if not not_found(exc):
            raise
    registered = with_role_propagation_retry(
        lambda: ecs.register_task_definition(
            **desired, tags=tags_list({"image-digest": image_digest})))
    return registered["taskDefinition"]["taskDefinitionArn"]


def build_and_push(app: dict, image: str, ecr) -> str:
    stage = stage_build_context(app)
    try:
        (stage / "Dockerfile").write_text(DOCKERFILE.format(app=app["name"]))
        auth = ecr.get_authorization_token()["authorizationData"][0]
        username, password = base64.b64decode(auth["authorizationToken"]).decode().split(":", 1)
        registry = auth["proxyEndpoint"]
        commands = [
            (["docker", "login", "--username", username, "--password-stdin", registry], password),
            (["docker", "build", "--platform", DOCKER_PLATFORM, "-t", image, str(stage)], None),
            (["docker", "push", image], None),
        ]
        for command, stdin in commands:
            proc = subprocess.run(command, input=stdin, text=True, check=False)
            if proc.returncode:
                fail(f"{' '.join(command[:2])} failed")
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    images = ecr.describe_images(
        repositoryName=REPOSITORY, imageIds=[{"imageTag": image.rsplit(":", 1)[1]}])
    return images["imageDetails"][0]["imageDigest"]


def cost_lines(logs, names: dict[str, str], region: str, cron: str,
               schedule_exists: bool) -> list[str]:
    print("Fetching list prices from the AWS price list...")
    try:
        runs = config.runs_per_month(cron)
        seconds, basis = run_basis(
            recent_stream_seconds(logs, names["log_group"]) if schedule_exists else None)
        seconds = max(seconds, FARGATE_MIN_SECONDS)
        vcpu = int(TASK_CPU) / 1024
        gib = int(TASK_MEMORY) / 1024
        vcpu_hour = list_price("AmazonECS", region, "Fargate-ARM-vCPU-Hours:perCPU")
        gib_hour = list_price("AmazonECS", region, "Fargate-ARM-GB-Hours")
        compute = runs * seconds / 3600 * (vcpu * vcpu_hour + gib * gib_hour)
        secret = list_price("AWSSecretsManager", region, "AWSSecretsManager-Secrets")
        items = [
            (f"Fargate (arm64): ~{runs:.0f} runs x {basis} x {vcpu:g} vCPU / {gib:g} GiB", compute),
            ("Secrets Manager: 1 secret", secret),
        ]
    except Exception as exc:
        fail(f"could not calculate the required monthly cost estimate: {exc}")
    return cost_estimate_lines(
        region, items,
        "excludes EventBridge Scheduler free tier, ECR storage, and CloudWatch Logs usage")


def fargate_clients(session) -> dict:
    clients = clients_for(session)
    clients.update({name: session.client(name) for name in ("ec2", "ecr", "ecs")})
    return clients


def deploy(app: dict, assume_yes: bool, profile: str | None = None) -> int:
    docker_preflight()
    session = ensure_session(app, profile)
    expected_account, region = aws_settings(app, session)
    clients = fargate_clients(session)
    account, _identity = preflight(
        clients["sts"], clients["iam"], expected_account, DEPLOYER_ACTIONS)
    names = resource_names(app["name"])
    cron = config.cron_expression(app["schedule"])
    expression = aws_schedule_expression(cron)
    payload = json.dumps(gather_secrets(app), sort_keys=True)
    image = f"{account}.dkr.ecr.{region}.amazonaws.com/{REPOSITORY}:{names['image_tag']}"

    print(f"Checking current state in account {account} ({region})...")
    subnets, security_group = default_network(clients["ec2"])
    schedule_exists = resource_exists(
        clients["scheduler"], "get_schedule",
        Name=names["schedule"], GroupName=SCHEDULE_GROUP)
    secret_exists = resource_exists(
        clients["secretsmanager"], "describe_secret", SecretId=names["secret"])
    actions = [
        f"reconcile shared ECR repository {REPOSITORY} and ECS cluster {CLUSTER}",
        f"build and push Docker image {image} ({DOCKER_PLATFORM})",
        ("update" if secret_exists else "create")
        + f" Secrets Manager secret {names['secret']}",
        "reconcile the execution, task, and scheduler IAM roles",
        f"reconcile Fargate task definition {names['family']} "
        f"({int(TASK_CPU) / 1024:g} vCPU, {TASK_MEMORY} MiB, no time limit)",
        ("update" if schedule_exists else "create")
        + f" EventBridge schedule {names['schedule']}: {expression} ({app['timezone']})",
        f"use default VPC subnets and security group {security_group} with a public IP",
    ]
    if not confirm(actions, assume_yes, cost_lines(
            clients["logs"], names, region, cron, schedule_exists)):
        print("Aborted; nothing was changed.")
        return 1

    print(f"==> reconciling AWS resources in {account} ({region})")
    repository_uri = ensure_repository(clients["ecr"])
    cluster_arn = ensure_cluster(clients["ecs"])
    ensure_log_group(clients["logs"], names["log_group"])
    secret_arn = ensure_secret(clients["secretsmanager"], names["secret"], payload)
    execution, task, scheduler_role = ensure_roles(
        clients["iam"], names, account, region, secret_arn)
    image = f"{repository_uri}:{names['image_tag']}"
    print(f"==> building and pushing {image}")
    image_digest = build_and_push(app, image, clients["ecr"])
    print("==> reconciling task definition and schedule")
    desired = desired_task(names, image, region, execution, task, secret_arn)
    task_arn = ensure_task_definition(clients["ecs"], desired, image_digest)
    target = {
        "Arn": cluster_arn,
        "EcsParameters": {
            "TaskDefinitionArn": task_arn,
            "LaunchType": "FARGATE",
            "TaskCount": 1,
            "NetworkConfiguration": {
                "awsvpcConfiguration": {
                    "Subnets": subnets,
                    "SecurityGroups": [security_group],
                    "AssignPublicIp": "ENABLED",
                },
            },
        },
    }
    ensure_schedule(clients["scheduler"], names["schedule"], expression,
                    app["timezone"], scheduler_role, target)
    print(f"Deployed {app['name']}.")
    print(f"Run it once: aws ecs run-task --cluster {CLUSTER} "
          f"--task-definition {names['family']} --launch-type FARGATE "
          f"--network-configuration 'awsvpcConfiguration={{subnets=[{subnets[0]}],"
          f"securityGroups=[{security_group}],assignPublicIp=ENABLED}}' --region {region}")
    return 0


def cluster_unused_after(ecs, family: str) -> bool:
    clusters = ecs.describe_clusters(clusters=[CLUSTER]).get("clusters", [])
    if not any(item.get("status") == "ACTIVE" for item in clusters):
        return False
    if ecs.list_tasks(cluster=CLUSTER).get("taskArns"):
        return False
    families = ecs.list_task_definition_families(
        familyPrefix="pdt-", status="ACTIVE").get("families", [])
    return all(item == family for item in families)


def repository_unused_after(ecr, image_tag: str) -> bool:
    try:
        images = ecr.list_images(repositoryName=REPOSITORY,
                                 filter={"tagStatus": "TAGGED"}).get("imageIds", [])
    except Exception as exc:
        if not not_found(exc):
            raise
        return False
    return all(item.get("imageTag") == image_tag for item in images)


def destroy(app: dict, assume_yes: bool, profile: str | None = None) -> int:
    session = ensure_session(app, profile)
    expected_account, region = aws_settings(app, session)
    clients = fargate_clients(session)
    account, _identity = preflight(
        clients["sts"], clients["iam"], expected_account, DEPLOYER_ACTIONS)
    names = resource_names(app["name"])
    schedule_exists = resource_exists(
        clients["scheduler"], "get_schedule",
        Name=names["schedule"], GroupName=SCHEDULE_GROUP)
    task_arns = clients["ecs"].list_task_definitions(
        familyPrefix=names["family"], status="ACTIVE").get("taskDefinitionArns", [])
    secret_exists = resource_exists(
        clients["secretsmanager"], "describe_secret", SecretId=names["secret"])
    actions = []
    if schedule_exists:
        actions.append(f"delete EventBridge schedule {names['schedule']}")
    if task_arns:
        actions.append(f"deregister {len(task_arns)} tagged task definition(s) "
                       f"in family {names['family']}")
    if secret_exists:
        actions.append(f"delete secret {names['secret']}")
    actions += [
        f"delete tagged log group {names['log_group']}",
        "delete tagged per-app IAM roles",
        f"delete image tag {names['image_tag']} from ECR repository {REPOSITORY}",
    ]
    others = other_schedules(clients["scheduler"], names["schedule"])
    if others == []:
        actions.append(f"delete schedule group {SCHEDULE_GROUP} (no other apps use it)")
    cluster_unused = cluster_unused_after(clients["ecs"], names["family"])
    if cluster_unused:
        actions.append(f"delete ECS cluster {CLUSTER} (no other apps use it)")
    repository_unused = repository_unused_after(clients["ecr"], names["image_tag"])
    if repository_unused:
        actions.append(f"delete ECR repository {REPOSITORY} (no other apps use it)")
    if not confirm(actions, assume_yes):
        print("Aborted; nothing was changed.")
        return 1

    if schedule_exists:
        clients["scheduler"].delete_schedule(
            Name=names["schedule"], GroupName=SCHEDULE_GROUP)
    ecs = clients["ecs"]
    for arn in task_arns:
        tags = ecs.list_tags_for_resource(resourceArn=arn).get("tags", [])
        if has_managed_tag(tags, "key", "value"):
            ecs.deregister_task_definition(taskDefinition=arn)
    delete_secret(clients["secretsmanager"], names["secret"])
    delete_log_group(clients["logs"], names["log_group"])
    iam = clients["iam"]
    for role in (names["scheduler_role"], names["task_role"], names["execution_role"]):
        delete_role(iam, role)
    try:
        clients["ecr"].batch_delete_image(
            repositoryName=REPOSITORY, imageIds=[{"imageTag": names["image_tag"]}])
    except Exception as exc:
        if not not_found(exc):
            raise
    if others == []:
        delete_schedule_group(clients["scheduler"])
    if cluster_unused:
        ecs.delete_cluster(cluster=CLUSTER)
    if repository_unused:
        clients["ecr"].delete_repository(repositoryName=REPOSITORY, force=True)
    print(f"Removed {app['name']} from account {account} ({region}).")
    return 0
