"""Deploy an app as an ECS Fargate task run by EventBridge Scheduler.

Entered through deploy_aws.py, which owns the uv script header, login, and permission handling.
"""

from __future__ import annotations

import base64
import json
import shutil
import subprocess

from pdt import config, console
from pdt.deploy import confirm
from pdt.deploy_aws import (
    COMMON_ACTIONS, SCHEDULE_GROUP, aws_schedule_expression,
    aws_settings, clients_for, cost_estimate, list_price, log_group_url,
    recent_stream_seconds, run_basis, deployer_store, ensure_session, ensure_store,
    not_found, has_managed_tag, other_schedules, preflight, resource_exists,
    store_cost, store_exists, store_statements, store_url,
)
from pdt.deploy_common import (
    CostEstimate, fail, gather_secrets, image_action, stage_build_context,
    store_kept_line, store_name, store_plan_lines, warn_if_locked,
    write_dockerfile)
from pdt.terraform import Deployment
from pdt.terraform_aws import (
    app_imports, base_configuration, environment, fargate_configuration,
    shared_configuration, shared_imports, write_secret_value,
)

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
    "ecr:ListTagsForResource",
    "ecr:UntagResource",
    "ecr:PutImageScanningConfiguration",
    "ecr:PutImageTagMutability",
    "ecr:PutImage",
    "ecr:TagResource",
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
    "ecs:UntagResource",
    "ecs:PutClusterCapacityProviders",
    "ecs:UpdateCluster",
    "ecs:UpdateClusterSettings",
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


def docker_preflight() -> None:
    if not shutil.which("docker"):
        fail("Docker is required to deploy to AWS; install Docker Desktop "
             "and run the same command again")
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


def build_and_push(app: dict, image: str, ecr) -> str:
    stage = stage_build_context(app)
    try:
        write_dockerfile(stage, app)
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


def cost_estimate_for(logs, names: dict[str, str], region: str, cron: str,
               schedule_exists: bool, usage: tuple[int, int] | None) -> CostEstimate:
    console.status("Fetching list prices from the AWS price list...")
    try:
        runs = config.runs_per_month(cron)
        seconds, basis = run_basis(
            recent_stream_seconds(clients["logs"], names["log_group"])
            if schedule_exists else None)
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
        if usage is not None:
            items.append(store_cost(usage, region))
    except Exception as exc:
        fail(f"could not calculate the required monthly cost estimate: {exc}")
    return cost_estimate(
        region, items,
        "excludes EventBridge Scheduler free tier, ECR storage, and CloudWatch Logs usage")


def fargate_clients(session) -> dict:
    clients = clients_for(session)
    clients.update({name: session.client(name) for name in ("ec2", "ecr", "ecs")})
    return clients


def cluster_unused_after(ecs, family: str) -> bool:
    clusters = ecs.describe_clusters(clusters=[CLUSTER]).get("clusters", [])
    if not any(item.get("status") == "ACTIVE" for item in clusters):
        return False
    if ecs.list_tasks(cluster=CLUSTER).get("taskArns"):
        return False
    pages = ecs.get_paginator("list_task_definition_families")
    for page in pages.paginate(familyPrefix="pdt-", status="ACTIVE"):
        if any(item != family for item in page.get("families", [])):
            return False
    return True


def repository_unused_after(ecr, image_tag: str) -> bool:
    try:
        pages = ecr.get_paginator("list_images")
        for page in pages.paginate(repositoryName=REPOSITORY, filter={"tagStatus": "TAGGED"}):
            if any(item.get("imageTag") != image_tag for item in page.get("imageIds", [])):
                return False
    except Exception as exc:
        if not not_found(exc):
            raise
        return False
    return True


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
    repository_uri = f"{account}.dkr.ecr.{region}.amazonaws.com/{REPOSITORY}"
    image = f"{repository_uri}:{names['image_tag']}"
    bucket = store_name(account)
    store = deployer_store(app, session, account) if app["storage"] else None

    console.status(f"Checking current state in account {account} ({region})...")
    store_present = store_exists(clients["s3"], bucket) if store else False
    usage = (store.usage() if store_present else (0, 0)) if store else None
    subnets, security_group = default_network(clients["ec2"])
    schedule_exists = resource_exists(
        clients["scheduler"], "get_schedule", Name=names["schedule"], GroupName=SCHEDULE_GROUP)
    actions = [
        "prepare protected Terraform state storage and deployment locking in AWS",
        f"reconcile shared ECR repository {REPOSITORY}, ECS cluster {CLUSTER}, and schedule group with Terraform",
        image_action(app, f"build and push Docker image {image} ({DOCKER_PLATFORM})"),
        f"reconcile secret {names['secret']}, log group {names['log_group']}, and IAM roles with Terraform",
        f"reconcile Fargate task definition {names['family']} "
        f"({int(TASK_CPU) / 1024:g} vCPU, {TASK_MEMORY} MiB, no time limit)",
        f"reconcile EventBridge schedule {names['schedule']}: {expression} ({app['timezone']})",
        f"use default VPC subnets and security group {security_group} with a public IP",
    ]
    if store:
        actions += store_plan_lines(f"bucket {bucket}", store_present, names["task_role"], app["name"])
    if not confirm(actions, assume_yes, cost_estimate_for(
            clients["logs"], names, region, cron, schedule_exists, usage)):
        console.warn("Aborted; nothing was changed.")
        return 1

    identity = {"account": account, "region": region, "runtime": "fargate"}
    with Deployment(app, "aws", identity, environment(session, region), assume_yes) as deployment:
        deployment.save()
        shared_ids = shared_imports(clients, fargate=True)
        imports = app_imports(clients, names, fargate=True, storage=bool(app["storage"]))
        shared = deployment.workspace("shared", shared_configuration(region, fargate=True), shared_ids, retain=True)
        shared.apply(shared.plan())
        variables = {}
        grants = []
        if store:
            # The data bucket outlives the app, so it stays outside the app's Terraform state.
            ensure_store(clients["s3"], bucket, region)
            variables["PDT_STORAGE_URL"] = store_url(bucket, app["name"])
            grants = store_statements(bucket, app["name"])
        console.step(f"building and pushing {image}")
        image_digest = build_and_push(app, image, clients["ecr"])
        base = base_configuration(names, region)
        base_imports = {key: value for key, value in imports.items()
                        if key.startswith(("aws_secretsmanager_secret.", "aws_cloudwatch_log_group."))}
        workspace = deployment.workspace("app", base, base_imports, retain=True)
        workspace.apply(workspace.plan())
        write_secret_value(clients["secretsmanager"], workspace.outputs()["secret_arn"], payload)
        document = fargate_configuration(
            app, names, account, region, f"{repository_uri}@{image_digest}", subnets, security_group,
            variables, grants)
        workspace.reconcile(document, imports)
        deployment.save()
    console.done(f"Deployed {app['name']}.")
    console.field("Run it once", f"pdt aws ecs run-task --cluster {CLUSTER} "
                  f"--task-definition {names['family']} --launch-type FARGATE "
                  f"--network-configuration 'awsvpcConfiguration={{subnets=[{subnets[0]}],"
                  f"securityGroups=[{security_group}],assignPublicIp=ENABLED}}' --region {region}")
    console.field("Run logs", log_group_url(region, names["log_group"]))
    return 0


def destroy(app: dict, assume_yes: bool, profile: str | None = None) -> int:
    session = ensure_session(app, profile)
    expected_account, region = aws_settings(app, session)
    clients = fargate_clients(session)
    account, _identity = preflight(
        clients["sts"], clients["iam"], expected_account, DEPLOYER_ACTIONS)
    names = resource_names(app["name"])
    actions = [
        "prepare protected Terraform state storage and deployment locking in AWS",
        f"delete Terraform-managed EventBridge schedule and ECS task definition {names['family']}",
        f"delete tagged secret {names['secret']}, log group {names['log_group']}, and per-app IAM roles",
        f"delete image tag {names['image_tag']} from ECR repository {REPOSITORY}",
        "delete shared resources when no other app uses them",
    ]
    bucket = store_name(account)
    store = deployer_store(app, session, account) if app["storage"] else None
    store_present = store_exists(clients["s3"], bucket) if store else False
    if store_present:
        warn_if_locked(store, app["name"])
    if not confirm(actions, assume_yes):
        console.warn("Aborted; nothing was changed.")
        return 1

    identity = {"account": account, "region": region, "runtime": "fargate"}
    with Deployment(app, "aws", identity, environment(session, region), assume_yes) as deployment:
        deployment.save()
        shared_ids = shared_imports(clients, fargate=True)
        imports = app_imports(clients, names, fargate=True, storage=bool(app["storage"]))
        document = fargate_configuration(
            app, names, account, region, "unused", [], "unused", {},
            store_statements(bucket, app["name"]) if store else [])
        workspace = deployment.workspace("app", document, imports)
        state = json.loads(workspace.run("show", "-json").stdout)
        tracked = {}
        for resource in state.get("values", {}).get("root_module", {}).get("resources", []):
            if resource.get("type") == "aws_ecs_task_definition":
                tracked[resource["values"]["arn"]] = resource["name"]
        definitions = clients["ecs"].get_paginator("list_task_definitions")
        for page in definitions.paginate(familyPrefix=names["family"], status="ACTIVE"):
            for arn in page.get("taskDefinitionArns", []):
                if arn.rsplit("/", 1)[-1].rsplit(":", 1)[0] != names["family"] or arn in tracked:
                    continue
                task = clients["ecs"].describe_task_definition(taskDefinition=arn, include=["TAGS"])
                if not has_managed_tag(task.get("tags", []), "key", "value"):
                    continue
                key = "revision_" + arn.rsplit(":", 1)[-1]
                document["resource"]["aws_ecs_task_definition"][key] = dict(
                    document["resource"]["aws_ecs_task_definition"]["app"])
                imports[f"aws_ecs_task_definition.{key}"] = arn
        workspace.configuration = document
        workspace.init()
        workspace.import_resources(imports)
        workspace.apply(workspace.plan(destroy=True))
        if "aws_ecr_repository.pdt" in shared_ids:
            try:
                clients["ecr"].batch_delete_image(
                    repositoryName=REPOSITORY, imageIds=[{"imageTag": names["image_tag"]}])
            except Exception as exc:
                if not not_found(exc):
                    raise
        remove = []
        if other_schedules(clients["scheduler"], names["schedule"]) == []:
            remove.append("aws_scheduler_schedule_group.pdt")
        if cluster_unused_after(clients["ecs"], names["family"]):
            remove += ["aws_ecs_cluster_capacity_providers.pdt", "aws_ecs_cluster.pdt"]
        if repository_unused_after(clients["ecr"], names["image_tag"]):
            remove.append("aws_ecr_repository.pdt")
        if remove:
            shared = deployment.existing_workspace("shared")
            if shared is None:
                document = shared_configuration(region, fargate=True)
                for kind, instances in document["resource"].items():
                    for name in list(instances):
                        if f"{kind}.{name}" not in shared_ids:
                            del instances[name]
                shared = deployment.workspace("shared", document, shared_ids)
            shared.remove_resources(remove)
        deployment.finish_destroy()
    console.done(f"Removed {app['name']} from account {account} ({region}).")
    if store_present:
        console.say(store_kept_line(f"bucket {bucket}", store.usage()[0], app["name"]))
    return 0
