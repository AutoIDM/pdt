"""Deploy an app as an AWS Batch job on Fargate, submitted by EventBridge Scheduler.

The shape. Per project, shared by every app in the account and region: one
Batch compute environment `pdt` (managed, Fargate, on demand), one job queue
`pdt`, the security group `pdt` in the default VPC (no inbound rules, all
outbound), the ECR repository `pdt`, the schedule group `pdt`, and the store
bucket. Per app: one job definition `pdt-<app>` (the app's image, arm64,
JOB_VCPU and JOB_MEMORY, the env secret through containerProperties.secrets,
an execution role and a job role), one log group `/pdt/<app>`, and one
schedule `pdt-<app>` whose target is the universal target
`arn:aws:scheduler:::aws-sdk:batch:submitJob`. Every resource is tagged
`managed-by=pdt` and named `pdt-<app>` or `pdt`.

Destroy also clears what a Fargate deployment of the same app left behind
(LEGACY_ACTIONS, legacy_fargate_cleanup); drop both once every project has
moved to Batch.

Entered through deploy_aws.py, which owns the uv script header, login, and
permission handling.
"""

from __future__ import annotations

import base64
import dataclasses
import json
import shutil
import subprocess
import time
from datetime import datetime, timedelta, timezone
from typing import Callable

from pdt import config, console, regions, runs_cli
from pdt.deploy import confirm
from pdt.deploy_aws import (
    COMMON_ACTIONS, MANAGED_TAGS, SCHEDULE_GROUP, aws_schedule_expression,
    aws_settings, clients_for, cost_estimate, delete_log_group, delete_role,
    delete_secret, list_price, log_group_url, recent_stream_seconds, run_basis,
    ensure_log_group, ensure_role, ensure_schedule, ensure_secret,
    deployer_store, ensure_session, ensure_store, find_log_group, has_managed_tag, iam_tags,
    delete_if_present, error_code, not_found,
    delete_schedule_group, other_schedules, preflight, resource_exists, retry, secret_statements,
    store_cost, store_exists, store_statements, store_url, with_role_propagation_retry,
)
from pdt.deploy_common import (
    CostEstimate, fail, gather_secrets, image_action, run_secrets, secret_contents,
    ssh_build_args, stage_build_context, store_kept_line, store_name, store_plan_lines,
    warn_if_locked,
    write_dockerfile,
)

REPOSITORY = "pdt"
SECURITY_GROUP = "pdt"
LEGACY_CLUSTER = "pdt"
JOB_VCPU = "0.25"
JOB_MEMORY = "512"
JOB_ARCHITECTURE = "ARM64"
DOCKER_PLATFORM = "linux/arm64"
FARGATE_MIN_SECONDS = 60
COMPUTE_MAX_VCPUS = 16
SUBMIT_JOB_TARGET = "arn:aws:scheduler:::aws-sdk:batch:submitJob"
JOB_STATUSES = {"SUCCEEDED": "succeeded", "FAILED": "failed"}
BATCH_WAIT_DELAYS = (2, 3, 5, 5, 10, 10, 15, 15, 15, *[30] * 18)
BATCH_ACTIONS = [
    "batch:CreateComputeEnvironment",
    "batch:CreateJobQueue",
    "batch:DeleteComputeEnvironment",
    "batch:DeleteJobQueue",
    "batch:DeregisterJobDefinition",
    "batch:DescribeComputeEnvironments",
    "batch:DescribeJobDefinitions",
    "batch:DescribeJobQueues",
    "batch:DescribeJobs",
    "batch:ListJobs",
    "batch:RegisterJobDefinition",
    "batch:TagResource",
    "batch:UpdateComputeEnvironment",
    "batch:UpdateJobQueue",
    "ec2:CreateSecurityGroup",
    "ec2:CreateTags",
    "ec2:DeleteSecurityGroup",
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
    "ecr:TagResource",
    "ecr:UploadLayerPart",
    "iam:CreateServiceLinkedRole",
]
# Destroy still removes what a Fargate deployment of the same app left behind.
LEGACY_ACTIONS = [
    "ecs:DeleteCluster",
    "ecs:DeregisterTaskDefinition",
    "ecs:DescribeClusters",
    "ecs:ListTagsForResource",
    "ecs:ListTaskDefinitionFamilies",
    "ecs:ListTaskDefinitions",
    "ecs:ListTasks",
]
DEPLOYER_ACTIONS = sorted(COMMON_ACTIONS + BATCH_ACTIONS + LEGACY_ACTIONS)


@dataclasses.dataclass(frozen=True)
class SharedResource:
    """A Batch resource every app shares, with the boto3 operations that drive it.

    The job queue and the compute environment have the same lifecycle:
    describe, enable, disable, delete, and wait for each to settle.
    """
    label: str
    name: str
    describe: str
    key: str
    update: str
    delete: str
    param: str
    arn: str


COMPUTE_ENVIRONMENT = SharedResource(
    "compute environment", "pdt", "describe_compute_environments", "computeEnvironments",
    "update_compute_environment", "delete_compute_environment", "computeEnvironment",
    "computeEnvironmentArn")
JOB_QUEUE = SharedResource(
    "job queue", "pdt", "describe_job_queues", "jobQueues",
    "update_job_queue", "delete_job_queue", "jobQueue", "jobQueueArn")


def resource_names(app_name: str) -> dict[str, str]:
    base = f"pdt-{app_name}"
    return {
        "job_definition": base,
        "schedule": base,
        "secret": f"{base}-env",
        "log_group": f"/pdt/{app_name}",
        "execution_role": f"{base}-execution",
        "job_role": f"{base}-job",
        "scheduler_role": f"{base}-scheduler",
        "image_tag": f"{app_name}-latest",
        "legacy_log_group": f"/ecs/{base}",
        "legacy_task_role": f"{base}-task",
    }


def managed(item: dict) -> bool:
    return (item.get("tags") or {}).get("managed-by") == "pdt"


def shared_tags() -> dict[str, str]:
    return {**MANAGED_TAGS, "shared": "true"}


def docker_preflight() -> None:
    if not shutil.which("docker"):
        fail("Docker is required to deploy to AWS; install Docker Desktop "
             "and run the same command again")
    proc = subprocess.run(["docker", "info"], capture_output=True, text=True, check=False)
    if proc.returncode:
        fail("Docker is installed but not running; start Docker and run the same command again")


def default_vpc(ec2) -> str | None:
    vpcs = ec2.describe_vpcs(Filters=[{"Name": "is-default", "Values": ["true"]}])["Vpcs"]
    return vpcs[0]["VpcId"] if vpcs else None


def default_network(ec2) -> tuple[str, list[str]]:
    vpc_id = default_vpc(ec2)
    if vpc_id is None:
        fail("this account/region has no default VPC; custom VPC configuration is not supported yet")
    subnets = ec2.describe_subnets(Filters=[
        {"Name": "vpc-id", "Values": [vpc_id]},
        {"Name": "state", "Values": ["available"]},
        {"Name": "default-for-az", "Values": ["true"]},
    ])["Subnets"]
    subnet_ids = sorted(subnet["SubnetId"] for subnet in subnets)
    if not subnet_ids:
        fail(f"default VPC {vpc_id} has no available default subnets")
    return vpc_id, subnet_ids


def find_security_group(ec2, vpc_id: str) -> dict | None:
    groups = ec2.describe_security_groups(Filters=[
        {"Name": "vpc-id", "Values": [vpc_id]},
        {"Name": "group-name", "Values": [SECURITY_GROUP]},
    ])["SecurityGroups"]
    return groups[0] if groups else None


def managed_group(group: dict | None) -> bool:
    return group is not None and has_managed_tag(group.get("Tags", []), "Key", "Value")


def ensure_security_group(ec2, vpc_id: str, group: dict | None) -> str:
    # A new group has no inbound rules and allows all outbound traffic, which is what a job needs.
    if group is not None:
        if not managed_group(group):
            fail(f"security group {SECURITY_GROUP} in VPC {vpc_id} exists but is not managed by PDT")
        return group["GroupId"]
    return ec2.create_security_group(
        GroupName=SECURITY_GROUP,
        Description="pdt jobs: no inbound, all outbound",
        VpcId=vpc_id,
        TagSpecifications=[{"ResourceType": "security-group", "Tags": iam_tags({"shared": "true"})}],
    )["GroupId"]


def delete_security_group(ec2, group_id: str) -> bool:
    """Delete the group; False when it is already gone.

    A job's network interface holds the group for a while after the job stops.
    """
    return retry(lambda: delete_if_present(ec2.delete_security_group, GroupId=group_id),
                 lambda exc: error_code(exc) == "DependencyViolation",
                 f"security group {SECURITY_GROUP} is still in use", BATCH_WAIT_DELAYS)


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


def wait_for_batch(describe: Callable[[], dict | None], ready: Callable[[dict | None], bool],
                   what: str, sleep=time.sleep) -> dict | None:
    for delay in (*BATCH_WAIT_DELAYS, None):
        current = describe()
        if ready(current):
            return current
        if delay is None:
            fail(f"gave up waiting for {what}")
        console.bullet(console.escape(f"waiting for {what}..."), indent=4)
        sleep(delay)
    raise AssertionError("unreachable")


def describe(batch, resource: SharedResource) -> dict | None:
    found = getattr(batch, resource.describe)(**{resource.key: [resource.name]})[resource.key]
    # Batch keeps a deleted queue or environment listed as DELETED for a while.
    return next((item for item in found if item.get("status") != "DELETED"), None)


def resting(item: dict | None) -> bool:
    return item is None or item["status"] in ("VALID", "INVALID")


def settled(batch, resource: SharedResource) -> dict | None:
    """The shared resource once Batch has finished changing it, or None when it is gone."""
    return wait_for_batch(lambda: describe(batch, resource), resting,
                          f"Batch {resource.label} {resource.name}")


def ensure_shared(batch, resource: SharedResource, create: Callable[[], None]) -> str:
    current = settled(batch, resource)
    if current is None:
        create()
    elif not managed(current):
        fail(f"Batch {resource.label} {resource.name} exists but is not managed by PDT")
    elif current["state"] != "ENABLED":
        getattr(batch, resource.update)(**{resource.param: resource.name}, state="ENABLED")
    elif current["status"] == "VALID":
        return current[resource.arn]
    ready = wait_for_batch(lambda: describe(batch, resource),
                           lambda item: item is not None and resting(item),
                           f"Batch {resource.label} {resource.name}")
    if ready["status"] == "INVALID":
        fail(f"Batch {resource.label} {resource.name} is invalid: {ready.get('statusReason', '')}")
    return ready[resource.arn]


def remove(batch, resource: SharedResource) -> bool:
    """Disable, then delete; False when a sibling destroy already removed the resource."""
    # Batch refuses to delete an enabled queue or compute environment, and
    # deletion is asynchronous, so each step waits for the one before it.
    try:
        if settled(batch, resource) is None:
            return False
        getattr(batch, resource.update)(**{resource.param: resource.name}, state="DISABLED")
        wait_for_batch(lambda: describe(batch, resource),
                       lambda item: item is None or (item["state"] == "DISABLED" and resting(item)),
                       f"Batch {resource.label} {resource.name} to be disabled")
    except Exception as exc:
        if not not_found(exc):
            raise
        return False
    if not delete_if_present(getattr(batch, resource.delete), **{resource.param: resource.name}):
        return False
    wait_for_batch(lambda: describe(batch, resource), lambda item: item is None,
                   f"Batch {resource.label} {resource.name} to be deleted")
    return True


def environment_security_groups(item: dict | None) -> list[str]:
    return sorted((item or {}).get("computeResources", {}).get("securityGroupIds") or [])


def ensure_compute_environment(batch, subnets: list[str], security_group: str,
                               move: bool) -> str:
    """`move` switches an existing environment to `security_group`, as the plan said."""
    def create():
        batch.create_compute_environment(
            computeEnvironmentName=COMPUTE_ENVIRONMENT.name,
            type="MANAGED",
            state="ENABLED",
            computeResources={"type": "FARGATE", "maxvCpus": COMPUTE_MAX_VCPUS,
                              "subnets": subnets, "securityGroupIds": [security_group]},
            tags=shared_tags(),
        )

    arn = ensure_shared(batch, COMPUTE_ENVIRONMENT, create)
    if not move:
        return arn
    batch.update_compute_environment(computeEnvironment=COMPUTE_ENVIRONMENT.name,
                                     computeResources={"securityGroupIds": [security_group]})
    return ensure_shared(batch, COMPUTE_ENVIRONMENT, create)


def ensure_job_queue(batch, environment_arn: str) -> str:
    return ensure_shared(batch, JOB_QUEUE, lambda: batch.create_job_queue(
        jobQueueName=JOB_QUEUE.name,
        state="ENABLED",
        priority=1,
        computeEnvironmentOrder=[{"order": 1, "computeEnvironment": environment_arn}],
        tags=shared_tags(),
    ))


def ensure_roles(iam, names: dict[str, str], account: str, region: str,
                 secret_arn: str, store_statements: list[dict]) -> tuple[str, str, str]:
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
    job = ensure_role(iam, names["job_role"], "ecs-tasks.amazonaws.com", "pdt-job",
                      secret_statements(secret_arn) + store_statements)
    scheduler = ensure_role(
        iam, names["scheduler_role"], "scheduler.amazonaws.com", "pdt-scheduler", [
            {"Effect": "Allow", "Action": ["batch:SubmitJob"],
             "Resource": submit_job_resources(names, account, region)},
        ])
    return execution, job, scheduler


def submit_job_resources(names: dict[str, str], account: str, region: str) -> list[str]:
    """What the schedule may submit: the job definition by name or by revision, and the queue."""
    definition = f"arn:aws:batch:{region}:{account}:job-definition/{names['job_definition']}"
    return [definition, f"{definition}:*",
            f"arn:aws:batch:{region}:{account}:job-queue/{JOB_QUEUE.name}"]


def desired_job_definition(names: dict[str, str], image: str, region: str,
                           execution_role: str, job_role: str, secret_arn: str,
                           environment: dict[str, str]) -> dict:
    return {
        "jobDefinitionName": names["job_definition"],
        "type": "container",
        "platformCapabilities": ["FARGATE"],
        "containerProperties": {
            "image": image,
            "jobRoleArn": job_role,
            "executionRoleArn": execution_role,
            "resourceRequirements": [{"type": "VCPU", "value": JOB_VCPU},
                                     {"type": "MEMORY", "value": JOB_MEMORY}],
            "environment": [{"name": name, "value": value}
                            for name, value in environment.items()],
            "secrets": [{"name": "PDT_ENV_JSON", "valueFrom": secret_arn}],
            "logConfiguration": {
                "logDriver": "awslogs",
                "options": {"awslogs-group": names["log_group"], "awslogs-region": region},
            },
            "networkConfiguration": {"assignPublicIp": "ENABLED"},
            "runtimePlatform": {"cpuArchitecture": JOB_ARCHITECTURE,
                                "operatingSystemFamily": "LINUX"},
        },
    }


def normalized_job_definition(definition: dict) -> dict:
    """The part of a job definition a deploy decides, in the shape Batch describes it."""
    container = definition.get("containerProperties") or {}
    log = container.get("logConfiguration") or {}
    return {
        "platformCapabilities": definition.get("platformCapabilities"),
        "image": container.get("image"),
        "jobRoleArn": container.get("jobRoleArn"),
        "executionRoleArn": container.get("executionRoleArn"),
        "resourceRequirements": sorted(container.get("resourceRequirements") or [],
                                       key=lambda item: item["type"]),
        "environment": sorted(container.get("environment") or [],
                              key=lambda item: item["name"]),
        "secrets": container.get("secrets"),
        "logConfiguration": {"logDriver": log.get("logDriver"), "options": log.get("options")},
        "networkConfiguration": container.get("networkConfiguration"),
        "runtimePlatform": container.get("runtimePlatform"),
    }


def same_job_definition(current: dict, desired: dict, image_digest: str) -> bool:
    return ((current.get("tags") or {}).get("image-digest") == image_digest
            and normalized_job_definition(current) == normalized_job_definition(desired))


def active_job_definitions(batch, name: str | None = None) -> list[dict]:
    """Active job definitions, every revision, newest first; all of them when no name is given."""
    kwargs = {"status": "ACTIVE", **({"jobDefinitionName": name} if name else {})}
    found = []
    while True:
        response = batch.describe_job_definitions(**kwargs)
        found += response.get("jobDefinitions", [])
        if "nextToken" not in response:
            break
        kwargs["nextToken"] = response["nextToken"]
    return sorted(found, key=lambda item: item["revision"], reverse=True)


def ensure_job_definition(batch, desired: dict, image_digest: str) -> str:
    revisions = active_job_definitions(batch, desired["jobDefinitionName"])
    if revisions:
        if not managed(revisions[0]):
            fail(f"Batch job definition {desired['jobDefinitionName']} exists "
                 "but is not managed by PDT")
        if same_job_definition(revisions[0], desired, image_digest):
            deregister_job_definitions(batch, revisions[1:])
            return revisions[0]["jobDefinitionArn"]
    registered = with_role_propagation_retry(lambda: batch.register_job_definition(
        **desired, tags={**MANAGED_TAGS, "image-digest": image_digest}))
    deregister_job_definitions(batch, revisions)
    return registered["jobDefinitionArn"]


def deregister_job_definitions(batch, revisions: list[dict]) -> None:
    for revision in revisions:
        if managed(revision):
            batch.deregister_job_definition(jobDefinition=revision["jobDefinitionArn"])


def other_job_definitions(batch, name: str) -> list[str]:
    """Names of the other apps' job definitions, which keep the shared queue and environment."""
    return sorted({item["jobDefinitionName"] for item in active_job_definitions(batch)
                   if item["jobDefinitionName"].startswith("pdt-")
                   and item["jobDefinitionName"] != name})


def shared_present(batch) -> list[SharedResource]:
    """The shared Batch resources that exist and that pdt manages, queue first."""
    found = {resource: describe(batch, resource) for resource in (JOB_QUEUE, COMPUTE_ENVIRONMENT)}
    return [resource for resource, item in found.items() if item is not None and managed(item)]


def submit_job_target(names: dict[str, str]) -> dict:
    # A universal target's input names the API parameters in PascalCase,
    # whatever casing the service's own API uses.
    return {
        "Arn": SUBMIT_JOB_TARGET,
        "Input": json.dumps({
            "JobName": names["job_definition"],
            "JobQueue": JOB_QUEUE.name,
            "JobDefinition": names["job_definition"],
        }, sort_keys=True),
    }


def build_and_push(app: dict, image: str, ecr) -> str:
    stage = stage_build_context(app)
    try:
        write_dockerfile(stage, app)
        auth = ecr.get_authorization_token()["authorizationData"][0]
        username, password = base64.b64decode(auth["authorizationToken"]).decode().split(":", 1)
        registry = auth["proxyEndpoint"]
        commands = [
            (["docker", "login", "--username", username, "--password-stdin", registry], password),
            (["docker", "build", "--platform", DOCKER_PLATFORM, *ssh_build_args(),
              "-t", image, str(stage)], None),
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
                      schedule_exists: bool, usage: tuple[int, int] | None,
                      local_currency: str) -> CostEstimate:
    console.status("Fetching list prices from the AWS price list...")
    try:
        runs = config.runs_per_month(cron)
        seconds, basis = run_basis(
            recent_stream_seconds(logs, names["log_group"]) if schedule_exists else None)
        seconds = max(seconds, FARGATE_MIN_SECONDS)
        vcpu = float(JOB_VCPU)
        gib = int(JOB_MEMORY) / 1024
        vcpu_hour = list_price("AmazonECS", region, "Fargate-ARM-vCPU-Hours:perCPU")
        gib_hour = list_price("AmazonECS", region, "Fargate-ARM-GB-Hours")
        compute = runs * seconds / 3600 * (vcpu * vcpu_hour + gib * gib_hour)
        secret = list_price("AWSSecretsManager", region, "AWSSecretsManager-Secrets")
        items = [
            (f"Batch on Fargate (arm64): ~{runs:.0f} runs x {basis} x {vcpu:g} vCPU / {gib:g} GiB",
             compute),
            ("Secrets Manager: 1 secret", secret),
        ]
        if usage is not None:
            items.append(store_cost(usage, region))
    except Exception as exc:
        fail(f"could not calculate the required monthly cost estimate: {exc}")
    return cost_estimate(
        region, items,
        "excludes EventBridge Scheduler free tier, ECR storage, and CloudWatch Logs usage",
        local_currency)


def batch_clients(session) -> dict:
    clients = clients_for(session)
    clients.update({name: session.client(name) for name in ("batch", "ec2", "ecr", "ecs")})
    return clients


def secrets(app: dict, action: str, assume_yes: bool, name: str | None = None) -> int:
    console.status("Checking your AWS sign-in...")
    session = ensure_session(app)
    expected_account, region = aws_settings(app, session)
    clients = batch_clients(session)
    preflight(clients["sts"], clients["iam"], expected_account, region, DEPLOYER_ACTIONS)
    client = clients["secretsmanager"]
    name = resource_names(app["name"])["secret"]
    current = None
    if resource_exists(client, "describe_secret", SecretId=name):
        current = client.get_secret_value(SecretId=name).get("SecretString", "")

    def write(values: dict[str, str]) -> None:
        console.step(f"updating secret {name}")
        ensure_secret(client, name, json.dumps(values, sort_keys=True))

    return run_secrets(action, app, current, write, assume_yes, name)


def deploy(app: dict, assume_yes: bool) -> int:
    docker_preflight()
    console.status("Checking your AWS sign-in...")
    session = ensure_session(app)
    expected_account, region = aws_settings(app, session)
    clients = batch_clients(session)
    account, _identity = preflight(
        clients["sts"], clients["iam"], expected_account, region, DEPLOYER_ACTIONS)
    names = resource_names(app["name"])
    cron = config.cron_expression(app["schedule"])
    expression = aws_schedule_expression(cron)
    secrets = gather_secrets(app)
    payload = json.dumps(secrets, sort_keys=True)
    image = f"{account}.dkr.ecr.{region}.amazonaws.com/{REPOSITORY}:{names['image_tag']}"
    bucket = store_name(account)
    store = deployer_store(app, session, account) if app["storage"] else None

    console.status(f"Checking current state in account {account} ({region})...")
    store_present = store_exists(clients["s3"], bucket) if store else False
    usage = (store.usage() if store_present else (0, 0)) if store else None
    vpc_id, subnets = default_network(clients["ec2"])
    group = find_security_group(clients["ec2"], vpc_id)
    group_id = group["GroupId"] if group else None
    # An environment made before pdt owned a group uses the VPC's default group.
    environment_groups = environment_security_groups(
        describe(clients["batch"], COMPUTE_ENVIRONMENT))
    move_environment = bool(environment_groups) and environment_groups != [group_id]
    schedule_exists = resource_exists(
        clients["scheduler"], "get_schedule",
        Name=names["schedule"], GroupName=SCHEDULE_GROUP)
    secret_exists = resource_exists(
        clients["secretsmanager"], "describe_secret", SecretId=names["secret"])
    actions = [
        f"reconcile shared ECR repository {REPOSITORY}, Batch compute environment "
        f"{COMPUTE_ENVIRONMENT.name}, and job queue {JOB_QUEUE.name}",
        image_action(app, f"build and push Docker image {image} ({DOCKER_PLATFORM})"),
        console.Markup(("update" if secret_exists else "create")
                       + f" Secrets Manager secret {console.escape(names['secret'])} "
                       f"({secret_contents(secrets)})"),
        "reconcile the execution, job, and scheduler IAM roles",
        f"allow {names['job_role']} to update its own secret {names['secret']}",
        f"reconcile Batch job definition {names['job_definition']} "
        f"({float(JOB_VCPU):g} vCPU, {JOB_MEMORY} MiB, no time limit)",
        ("update" if schedule_exists else "create")
        + f" EventBridge schedule {names['schedule']}: {expression} ({app['timezone']})",
        (f"use security group {SECURITY_GROUP} ({group_id})" if group else
         f"create security group {SECURITY_GROUP} (no inbound rules, all outbound)")
        + f" in default VPC {vpc_id}",
        "run jobs in the default VPC subnets with a public IP",
    ]
    if move_environment:
        actions.append(f"move Batch compute environment {COMPUTE_ENVIRONMENT.name} from "
                       f"security group {', '.join(environment_groups)} to {SECURITY_GROUP}")
    if store:
        actions += store_plan_lines(f"bucket {bucket}", store_present, names["job_role"], app["name"])
    if not confirm(actions, assume_yes, cost_estimate_for(
            clients["logs"], names, region, cron, schedule_exists, usage,
            regions.local_currency())):
        console.warn("Aborted; nothing was changed.")
        return 1

    console.step(f"reconciling AWS resources in {account} ({region})")
    repository_uri = ensure_repository(clients["ecr"])
    security_group = ensure_security_group(clients["ec2"], vpc_id, group)
    environment_arn = ensure_compute_environment(
        clients["batch"], subnets, security_group, move_environment)
    ensure_job_queue(clients["batch"], environment_arn)
    ensure_log_group(clients["logs"], names["log_group"])
    secret_arn = ensure_secret(clients["secretsmanager"], names["secret"], payload)
    environment = {"PDT_ENV_SECRET_RESOURCE": secret_arn}
    grants = []
    if store:
        ensure_store(clients["s3"], bucket, region)
        environment["PDT_STORAGE_URL"] = store_url(bucket, app["name"])
        grants = store_statements(bucket, app["name"])
    execution, job_role, scheduler_role = ensure_roles(
        clients["iam"], names, account, region, secret_arn, grants)
    image = f"{repository_uri}:{names['image_tag']}"
    console.step(f"building and pushing {image}")
    image_digest = build_and_push(app, image, clients["ecr"])
    console.step("reconciling job definition and schedule")
    desired = desired_job_definition(
        names, image, region, execution, job_role, secret_arn, environment)
    ensure_job_definition(clients["batch"], desired, image_digest)
    ensure_schedule(clients["scheduler"], names["schedule"], expression,
                    app["timezone"], scheduler_role, submit_job_target(names))
    console.done(f"Deployed {app['name']}.")
    console.field("Run it once", f"pdt aws batch submit-job --job-name {names['job_definition']} "
                  f"--job-queue {JOB_QUEUE.name} --job-definition {names['job_definition']} "
                  f"--region {region}")
    console.field("Run logs", log_group_url(region, names["log_group"]))
    return 0


def cluster_unused_after(ecs, family: str) -> bool:
    clusters = ecs.describe_clusters(clusters=[LEGACY_CLUSTER]).get("clusters", [])
    if not any(item.get("status") == "ACTIVE" for item in clusters):
        return False
    if ecs.list_tasks(cluster=LEGACY_CLUSTER).get("taskArns"):
        return False
    families = ecs.list_task_definition_families(
        familyPrefix="pdt-", status="ACTIVE").get("families", [])
    return all(item == family for item in families)


def legacy_task_definitions(ecs, family: str) -> list[str]:
    arns = ecs.list_task_definitions(
        familyPrefix=family, status="ACTIVE").get("taskDefinitionArns", [])
    return [arn for arn in arns if arn.rsplit("/", 1)[-1].rsplit(":", 1)[0] == family]


def deregister_task_definitions(ecs, arns: list[str]) -> None:
    for arn in arns:
        tags = ecs.list_tags_for_resource(resourceArn=arn).get("tags", [])
        if has_managed_tag(tags, "key", "value"):
            ecs.deregister_task_definition(taskDefinition=arn)


def legacy_fargate_cleanup(ecs, logs, iam, names: dict[str, str]) -> list[tuple[str, Callable]]:
    """One plan line and its action per leftover of a Fargate deployment of the same app."""
    family = names["job_definition"]
    plan = []
    arns = legacy_task_definitions(ecs, family)
    if arns:
        plan.append((f"deregister {len(arns)} tagged ECS task definition(s) in family {family} "
                     "(older Fargate deployment)",
                     lambda: deregister_task_definitions(ecs, arns)))
    if find_log_group(logs, names["legacy_log_group"]) is not None:
        plan.append((f"delete tagged log group {names['legacy_log_group']} "
                     "(older Fargate deployment)",
                     lambda: delete_log_group(logs, names["legacy_log_group"])))
    if resource_exists(iam, "get_role", RoleName=names["legacy_task_role"]):
        plan.append((f"delete tagged IAM role {names['legacy_task_role']} "
                     "(older Fargate deployment)",
                     lambda: delete_role(iam, names["legacy_task_role"])))
    if cluster_unused_after(ecs, family):
        plan.append((f"delete ECS cluster {LEGACY_CLUSTER} "
                     "(older Fargate deployment, no other apps use it)",
                     lambda: note_if_gone(f"ECS cluster {LEGACY_CLUSTER}", delete_if_present(
                         ecs.delete_cluster, cluster=LEGACY_CLUSTER))))
    return plan


def repository_unused_after(ecr, image_tag: str) -> bool:
    try:
        images = ecr.list_images(repositoryName=REPOSITORY,
                                 filter={"tagStatus": "TAGGED"}).get("imageIds", [])
    except Exception as exc:
        if not not_found(exc):
            raise
        return False
    return all(item.get("imageTag") == image_tag for item in images)


def note_if_gone(label: str, deleted: bool) -> None:
    if not deleted:
        console.note(f"{label} was already gone")


def destroy(app: dict, assume_yes: bool) -> int:
    console.status("Checking your AWS sign-in...")
    session = ensure_session(app)
    expected_account, region = aws_settings(app, session)
    clients = batch_clients(session)
    account, _identity = preflight(
        clients["sts"], clients["iam"], expected_account, region, DEPLOYER_ACTIONS)
    names = resource_names(app["name"])
    batch, scheduler, iam = clients["batch"], clients["scheduler"], clients["iam"]
    plan: list[tuple[str, Callable]] = []
    if resource_exists(scheduler, "get_schedule",
                       Name=names["schedule"], GroupName=SCHEDULE_GROUP):
        plan.append((f"delete EventBridge schedule {names['schedule']}",
                     lambda: scheduler.delete_schedule(
                         Name=names["schedule"], GroupName=SCHEDULE_GROUP)))
    revisions = [revision for revision in active_job_definitions(batch, names["job_definition"])
                 if managed(revision)]
    if revisions:
        plan.append((f"deregister {len(revisions)} tagged revision(s) of Batch job definition "
                     f"{names['job_definition']}",
                     lambda: deregister_job_definitions(batch, revisions)))
    if resource_exists(clients["secretsmanager"], "describe_secret", SecretId=names["secret"]):
        plan.append((f"delete secret {names['secret']}",
                     lambda: delete_secret(clients["secretsmanager"], names["secret"])))
    plan += [
        (f"delete tagged log group {names['log_group']}",
         lambda: delete_log_group(clients["logs"], names["log_group"])),
        ("delete tagged per-app IAM roles",
         lambda: [delete_role(iam, role) for role in
                  (names["scheduler_role"], names["job_role"], names["execution_role"])]),
        (f"delete image tag {names['image_tag']} from ECR repository {REPOSITORY}",
         lambda: note_if_gone(
             f"image tag {names['image_tag']} in ECR repository {REPOSITORY}",
             delete_if_present(clients["ecr"].batch_delete_image, repositoryName=REPOSITORY,
                               imageIds=[{"imageTag": names["image_tag"]}]))),
    ]
    plan += legacy_fargate_cleanup(clients["ecs"], clients["logs"], iam, names)
    if other_schedules(scheduler, names["schedule"]) == []:
        plan.append((f"delete schedule group {SCHEDULE_GROUP} (no other apps use it)",
                     lambda: note_if_gone(f"schedule group {SCHEDULE_GROUP}",
                                          delete_schedule_group(scheduler))))
    if not other_job_definitions(batch, names["job_definition"]):
        for resource in shared_present(batch):
            plan.append((f"delete Batch {resource.label} {resource.name} (no other apps use it)",
                         lambda resource=resource: note_if_gone(
                             f"Batch {resource.label} {resource.name}", remove(batch, resource))))
        vpc_id = default_vpc(clients["ec2"])
        group = find_security_group(clients["ec2"], vpc_id) if vpc_id else None
        if managed_group(group):
            plan.append((f"delete security group {SECURITY_GROUP} ({group['GroupId']}) "
                         "(no other apps use it)",
                         lambda: note_if_gone(f"security group {SECURITY_GROUP}",
                                              delete_security_group(clients["ec2"], group["GroupId"]))))
    if repository_unused_after(clients["ecr"], names["image_tag"]):
        plan.append((f"delete ECR repository {REPOSITORY} (no other apps use it)",
                     lambda: note_if_gone(f"ECR repository {REPOSITORY}", delete_if_present(
                         clients["ecr"].delete_repository, repositoryName=REPOSITORY,
                         force=True))))
    bucket = store_name(account)
    store = deployer_store(app, session, account) if app["storage"] else None
    store_present = store_exists(clients["s3"], bucket) if store else False
    if store_present:
        warn_if_locked(store, app["name"])
    if not confirm([line for line, _action in plan], assume_yes):
        console.warn("Aborted; nothing was changed.")
        return 1

    for _line, action in plan:
        action()
    console.done(f"Removed {app['name']} from account {account} ({region}).")
    if store_present:
        console.say(store_kept_line(f"bucket {bucket}", store.usage()[0], app["name"]))
    return 0


def ms_to_utc(milliseconds: int) -> datetime:
    return datetime.fromtimestamp(milliseconds / 1000, tz=timezone.utc)


def job_run(job: dict) -> runs_cli.Run:
    """A Batch job summary as a Run; a job that has not started yet counts from its submission."""
    status = JOB_STATUSES.get(job["status"], "running")
    started = ms_to_utc(job.get("startedAt") or job["createdAt"])
    ended = ms_to_utc(job["stoppedAt"]) if status != "running" and "stoppedAt" in job else None
    return runs_cli.Run(job["jobId"], started, ended, status,
                        (job.get("container") or {}).get("exitCode"))


def list_runs(batch, job_name: str) -> list[runs_cli.Run]:
    kwargs = {"jobQueue": JOB_QUEUE.name,
              "filters": [{"name": "JOB_NAME", "values": [job_name]}]}
    found = []
    while True:
        try:
            response = batch.list_jobs(**kwargs)
        except Exception as exc:
            if not_found(exc):
                return []
            raise
        found += [job_run(job) for job in response.get("jobSummaryList", [])]
        if "nextToken" not in response:
            break
        kwargs["nextToken"] = response["nextToken"]
    found.sort(key=lambda run: run.started, reverse=True)
    return found


def log_stream(batch, run: runs_cli.Run) -> str | None:
    jobs = batch.describe_jobs(jobs=[run.id]).get("jobs", [])
    return (jobs[0].get("container") or {}).get("logStreamName") if jobs else None


def read_lines(batch, logs, log_group: str, run: runs_cli.Run) -> list[runs_cli.Line]:
    stream = log_stream(batch, run)
    if stream is None:
        return []
    lines = []
    token = None
    while True:
        kwargs = {"logGroupName": log_group, "logStreamName": stream, "startFromHead": True}
        if token is not None:
            kwargs["nextToken"] = token
        try:
            response = logs.get_log_events(**kwargs)
        except Exception as exc:
            if not_found(exc):
                return lines
            raise
        for event in response["events"]:
            lines.append(runs_cli.parse_line(event["message"], ms_to_utc(event["timestamp"])))
        next_token = response["nextForwardToken"]
        if not response["events"] or next_token == token:
            return lines
        token = next_token


def runs(app: dict, session, rest: list[str]) -> int:
    names = resource_names(app["name"])
    batch = session.client("batch")
    return runs_cli.runs(lambda: list_runs(batch, names["job_definition"]), app["name"], rest)


def logs(app: dict, session, rest: list[str]) -> int:
    names = resource_names(app["name"])
    batch, logs_client = session.client("batch"), session.client("logs")
    return runs_cli.logs(
        lambda: list_runs(batch, names["job_definition"]),
        lambda run: read_lines(batch, logs_client, names["log_group"], run),
        app["name"], rest, store="CloudWatch Logs", delay=runs_cli.LOG_DELAY)
