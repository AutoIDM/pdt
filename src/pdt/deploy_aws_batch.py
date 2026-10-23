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

When CodeBuild builds the image (deploy_common.build_in_cloud), the
account and region also hold the CodeBuild project `pdt` (ARM_CONTAINER,
privileged for docker), its service role `pdt-codebuild`, the log group
BUILD_LOG_GROUP, and the private bucket `pdt-build-<suffix>` whose objects
expire after a day. A deploy uploads the staged context to that bucket as
a zip, builds and pushes the image with one build, and deletes the upload.
A deploy that builds with Docker on this computer creates none of them.
Destroy removes them with the shared ECR repository.

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
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterator

from botocore.exceptions import BotoCoreError, ClientError

from pdt import config, console, lifecycle, regions, runs_cli
from pdt.deploy import DECLINED, confirm, show_plan
from pdt.deploy_aws import (
    BUILD_BUCKET_PREFIX, COMMON_ACTIONS, MANAGED_TAGS, RETRIES, SCHEDULE_GROUP,
    aws_schedule_expression,
    aws_settings, clients_for, cost_estimate, delete_log_group, delete_role,
    delete_secret, list_price, recent_stream_seconds, run_basis,
    ensure_log_group, ensure_role, ensure_schedule, ensure_secret,
    deployer_store, ensure_session, ensure_store, find_log_group, has_managed_tag, iam_tags,
    console_note, console_url, log_group_url,
    delete_if_present, error_code, not_found,
    delete_schedule_group, other_schedules, preflight, resource_exists, retry, secret_statements,
    store_cost, store_exists, store_statements, store_url, with_role_propagation_retry,
)
from pdt.deploy_common import (
    BUILD_TAG, CostEstimate, build_in_cloud, ensure_image, fail, gather_secrets, image_action,
    run_build, run_secrets, secret_contents, ssh_build_args, store_kept_line,
    store_name, store_plan_lines, store_suffix, warn_if_locked,
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
# Measured in October 2026: CloudWatch Logs showed a line at most 7.0 s after the write.
HOLD = timedelta(seconds=10)
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
    "batch:SubmitJob",
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
CODEBUILD_ACTIONS = [
    "codebuild:BatchGetBuilds",
    "codebuild:BatchGetProjects",
    "codebuild:CreateProject",
    "codebuild:DeleteProject",
    "codebuild:StartBuild",
    "codebuild:UpdateProject",
    "s3:DeleteBucket",
    "s3:DeleteObject",
    "s3:PutLifecycleConfiguration",
    "s3:PutObject",
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
DEPLOYER_ACTIONS = sorted(COMMON_ACTIONS + BATCH_ACTIONS + CODEBUILD_ACTIONS + LEGACY_ACTIONS)
BUILD_PROJECT = "pdt"
BUILD_ROLE = "pdt-codebuild"
# Under /pdt/ so the deployer's log grants cover it, and two levels deep so no app's
# /pdt/<app> group can take the name.
BUILD_LOG_GROUP = "/pdt/shared/codebuild"
BUILD_IMAGE = "aws/codebuild/amazonlinux-aarch64-standard:3.0"
BUILD_COMPUTE = "BUILD_GENERAL1_SMALL"
BUILD_POLL_SECONDS = 5
# CloudWatch Logs shows a line some seconds after CodeBuild writes it.
BUILD_LOG_SETTLE_SECONDS = 10
BUILDSPEC = f"""\
version: 0.2
phases:
  pre_build:
    commands:
      - aws ecr get-login-password | docker login --username AWS --password-stdin "${{IMAGE%%/*}}"
  build:
    commands:
      - docker build --platform {DOCKER_PLATFORM} -t "$IMAGE" .
      - docker push "$IMAGE"
"""


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
        fail(f"default VPC {console.value(vpc_id)} has no available default subnets")
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
            fail(f"security group {console.value(SECURITY_GROUP)} in VPC {console.value(vpc_id)} "
                 "exists but is not managed by PDT")
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
                 f"security group {console.value(SECURITY_GROUP)}", "in use", "deleted",
                 BATCH_WAIT_DELAYS)


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
                   wait: console.WaitOn, state: str, sleep=time.sleep) -> dict | None:
    for delay in (*BATCH_WAIT_DELAYS, None):
        current = describe()
        if ready(current):
            return current
        shown = state or current["status"].lower()
        if delay is None:
            fail(f"gave up waiting for {wait.name}; it is still {shown}")
        wait.now(shown)
        sleep(delay)
    raise AssertionError("unreachable")


def describe(batch, resource: SharedResource) -> dict | None:
    found = getattr(batch, resource.describe)(**{resource.key: [resource.name]})[resource.key]
    # Batch keeps a deleted queue or environment listed as DELETED for a while.
    return next((item for item in found if item.get("status") != "DELETED"), None)


def resting(item: dict | None) -> bool:
    return item is None or item["status"] in ("VALID", "INVALID")


def settled(batch, resource: SharedResource, wait: console.WaitOn) -> dict | None:
    """The shared resource once Batch has finished changing it, or None when it is gone."""
    return wait_for_batch(lambda: describe(batch, resource), resting, wait, "")


def ensure_shared(batch, resource: SharedResource, create: Callable[[], None]) -> str:
    with console.wait_on(f"Batch {resource.label} {console.value(resource.name)}",
                         "ready") as wait:
        current = settled(batch, resource, wait)
        if current is None:
            create()
        elif not managed(current):
            fail(f"Batch {resource.label} {console.value(resource.name)} exists but is not "
                 "managed by PDT")
        elif current["state"] != "ENABLED":
            getattr(batch, resource.update)(**{resource.param: resource.name}, state="ENABLED")
        elif current["status"] == "VALID":
            return current[resource.arn]
        ready = wait_for_batch(lambda: describe(batch, resource),
                               lambda item: item is not None and resting(item),
                               wait, "creating" if current is None else "enabling")
        if ready["status"] == "INVALID":
            fail(f"Batch {resource.label} {console.value(resource.name)} is invalid: "
                 f"{console.escape(ready.get('statusReason', ''))}")
        return ready[resource.arn]


def remove(batch, resource: SharedResource) -> bool:
    """Disable, then delete; False when a sibling destroy already removed the resource."""
    # Batch refuses to delete an enabled queue or compute environment, and
    # deletion is asynchronous, so each step waits for the one before it.
    with console.wait_on(f"Batch {resource.label} {console.value(resource.name)}",
                         "deleted") as wait:
        try:
            if settled(batch, resource, wait) is None:
                return False
            getattr(batch, resource.update)(**{resource.param: resource.name}, state="DISABLED")
            wait_for_batch(lambda: describe(batch, resource),
                           lambda item: item is None or (item["state"] == "DISABLED"
                                                         and resting(item)),
                           wait, "disabling")
        except Exception as exc:
            if not not_found(exc):
                raise
            return False
        if not delete_if_present(getattr(batch, resource.delete),
                                 **{resource.param: resource.name}):
            return False
        wait_for_batch(lambda: describe(batch, resource), lambda item: item is None,
                       wait, "deleting")
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


def same_job_definition(current: dict, desired: dict, image_digest: str,
                        fingerprint: str) -> bool:
    tags = current.get("tags") or {}
    return (tags.get("image-digest") == image_digest and tags.get(BUILD_TAG) == fingerprint
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


def ensure_job_definition(batch, desired: dict, image_digest: str, fingerprint: str) -> str:
    revisions = active_job_definitions(batch, desired["jobDefinitionName"])
    if revisions:
        if not managed(revisions[0]):
            fail(f"Batch job definition {console.value(desired['jobDefinitionName'])} exists "
                 "but is not managed by PDT")
        if same_job_definition(revisions[0], desired, image_digest, fingerprint):
            deregister_job_definitions(batch, revisions[1:])
            return revisions[0]["jobDefinitionArn"]
    registered = with_role_propagation_retry(lambda: batch.register_job_definition(
        **desired, tags={**MANAGED_TAGS, "image-digest": image_digest, BUILD_TAG: fingerprint}))
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


def pushed_digest(ecr, image: str) -> str | None:
    """The digest of the image at `image`'s tag, or None when the repository has no such tag."""
    try:
        images = ecr.describe_images(
            repositoryName=REPOSITORY, imageIds=[{"imageTag": image.rsplit(":", 1)[1]}])
    except ClientError as exc:
        if not_found(exc):
            return None
        raise
    return images["imageDetails"][0]["imageDigest"]


def docker_build(stage: Path, image: str, ecr) -> None:
    auth = ecr.get_authorization_token()["authorizationData"][0]
    username, password = base64.b64decode(auth["authorizationToken"]).decode().split(":", 1)
    registry = auth["proxyEndpoint"]
    run_build(["docker", "login", "--username", username, "--password-stdin", registry],
              data=password, quiet=True)
    run_build(["docker", "build", "--platform", DOCKER_PLATFORM, *ssh_build_args(),
               "-t", image, str(stage)])
    run_build(["docker", "push", image])


def build_bucket(account: str, region: str) -> str:
    # CodeBuild reads its source only from a bucket in the build's own region.
    return f"{BUILD_BUCKET_PREFIX}-{store_suffix(f'{account}/{region}')}"


def build_project(codebuild) -> dict | None:
    projects = codebuild.batch_get_projects(names=[BUILD_PROJECT]).get("projects", [])
    return projects[0] if projects else None


def build_statements(account: str, region: str, bucket: str) -> list[dict]:
    """What the CodeBuild role may do: push to the pdt repository, write its log, read uploads."""
    return [
        {"Effect": "Allow", "Action": ["ecr:GetAuthorizationToken"], "Resource": "*"},
        {"Effect": "Allow",
         "Action": ["ecr:BatchCheckLayerAvailability", "ecr:CompleteLayerUpload",
                    "ecr:InitiateLayerUpload", "ecr:PutImage", "ecr:UploadLayerPart"],
         "Resource": f"arn:aws:ecr:{region}:{account}:repository/{REPOSITORY}"},
        {"Effect": "Allow", "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
         "Resource": f"arn:aws:logs:{region}:{account}:log-group:{BUILD_LOG_GROUP}:*"},
        {"Effect": "Allow", "Action": ["s3:GetObject", "s3:GetObjectVersion"],
         "Resource": f"arn:aws:s3:::{bucket}/*"},
        {"Effect": "Allow", "Action": ["s3:GetBucketAcl", "s3:GetBucketLocation"],
         "Resource": f"arn:aws:s3:::{bucket}"},
    ]


def desired_build_project(role_arn: str) -> dict:
    return {
        "name": BUILD_PROJECT,
        "description": "Builds pdt job images",
        # Each build names its upload with sourceTypeOverride and sourceLocationOverride.
        "source": {"type": "NO_SOURCE", "buildspec": BUILDSPEC},
        "artifacts": {"type": "NO_ARTIFACTS"},
        "environment": {"type": "ARM_CONTAINER", "image": BUILD_IMAGE,
                        "computeType": BUILD_COMPUTE, "privilegedMode": True},
        "serviceRole": role_arn,
        "logsConfig": {"cloudWatchLogs": {"status": "ENABLED", "groupName": BUILD_LOG_GROUP},
                       "s3Logs": {"status": "DISABLED"}},
        "tags": [{"key": key, "value": value} for key, value in shared_tags().items()],
    }


def ensure_build_resources(clients: dict, account: str, region: str, bucket: str,
                           project: dict | None) -> None:
    ensure_log_group(clients["logs"], BUILD_LOG_GROUP)
    ensure_store(clients["s3"], bucket, region, MANAGED_TAGS)
    clients["s3"].put_bucket_lifecycle_configuration(Bucket=bucket, LifecycleConfiguration={
        "Rules": [{"ID": "pdt-expire-build-uploads", "Status": "Enabled",
                   "Filter": {"Prefix": ""}, "Expiration": {"Days": 1}}]})
    role = ensure_role(clients["iam"], BUILD_ROLE, "codebuild.amazonaws.com", "pdt-codebuild",
                       build_statements(account, region, bucket))
    codebuild = clients["codebuild"]
    desired = desired_build_project(role)
    if project is None:
        with_role_propagation_retry(lambda: codebuild.create_project(**desired))
    elif not has_managed_tag(project.get("tags", []), "key", "value"):
        fail(f"CodeBuild project {console.value(BUILD_PROJECT)} exists but is not managed by PDT")
    else:
        with_role_propagation_retry(lambda: codebuild.update_project(**desired))


def show_build_log(logs, where: dict, token: str | None) -> str | None:
    """Print the build log lines after `token`; return the token to read on from."""
    group, stream = where.get("groupName"), where.get("streamName")
    if not group or not stream:
        return token
    while True:
        kwargs = {"logGroupName": group, "logStreamName": stream, "startFromHead": True}
        if token is not None:
            kwargs["nextToken"] = token
        try:
            response = logs.get_log_events(**kwargs)
        except Exception as exc:
            if not not_found(exc):
                raise
            return token
        for event in response["events"]:
            console.bullet(console.escape(event["message"].rstrip()), indent=4)
        if not response["events"] or response["nextForwardToken"] == token:
            return response["nextForwardToken"]
        token = response["nextForwardToken"]


def follow_build(codebuild, logs, build_id: str) -> dict:
    """Wait for the build, printing its log as it arrives; return the finished build."""
    token = None
    with console.wait_on(f"CodeBuild build {console.value(build_id)}", "",
                         f"reading: CodeBuild build {console.value(build_id)}") as wait:
        while True:
            build = codebuild.batch_get_builds(ids=[build_id])["builds"][0]
            if build.get("buildComplete") or (build.get("logs") or {}).get("streamName"):
                break
            wait.now(str(build.get("currentPhase") or "QUEUED").lower())
            time.sleep(BUILD_POLL_SECONDS)
    while True:
        build = codebuild.batch_get_builds(ids=[build_id])["builds"][0]
        if build.get("buildComplete"):
            time.sleep(BUILD_LOG_SETTLE_SECONDS)
            show_build_log(logs, build.get("logs") or {}, token)
            return build
        token = show_build_log(logs, build.get("logs") or {}, token)
        time.sleep(BUILD_POLL_SECONDS)


def failed_phase(build: dict) -> tuple[str, str]:
    """The phase that failed and what CodeBuild said about it."""
    for phase in build.get("phases") or []:
        if phase.get("phaseStatus") not in (None, "SUCCEEDED"):
            reason = "; ".join(context["message"] for context in phase.get("contexts") or []
                               if context.get("message"))
            return phase["phaseType"], reason or phase["phaseStatus"]
    return build.get("currentPhase", "unknown"), build.get("buildStatus", "unknown")


def codebuild_build(stage: Path, image: str, clients: dict, bucket: str, app_name: str) -> None:
    """Build and push `image` from `stage` with one CodeBuild build."""
    key = f"{app_name}/{uuid.uuid4().hex}.zip"
    archive = Path(shutil.make_archive(f"{stage}-source", "zip", root_dir=stage))
    try:
        clients["s3"].upload_file(str(archive), bucket, key)
    finally:
        archive.unlink(missing_ok=True)
    try:
        build = clients["codebuild"].start_build(
            projectName=BUILD_PROJECT, sourceTypeOverride="S3",
            sourceLocationOverride=f"{bucket}/{key}", buildspecOverride=BUILDSPEC,
            environmentVariablesOverride=[{"name": "IMAGE", "value": image,
                                           "type": "PLAINTEXT"}])["build"]
        console.status(f"CodeBuild build {console.value(build['id'])} started; its log follows.")
        build = follow_build(clients["codebuild"], clients["logs"], build["id"])
    finally:
        clients["s3"].delete_object(Bucket=bucket, Key=key)
    if build["buildStatus"] != "SUCCEEDED":
        phase, reason = failed_phase(build)
        fail(f"CodeBuild build {console.value(build['id'])} failed in phase "
             f"{console.value(phase)}: {console.escape(reason)}. The build log is above.")


def build_cleanup(clients: dict, bucket: str) -> list[tuple[str, Callable]]:
    """One plan line and its action per CodeBuild resource that exists and that pdt manages."""
    plan = []
    project = build_project(clients["codebuild"])
    if project is not None and has_managed_tag(project.get("tags", []), "key", "value"):
        plan.append((console.Markup(f"delete CodeBuild project {console.value(BUILD_PROJECT)} "
                                    "(no other apps use it)"),
                     lambda: note_if_gone(f"CodeBuild project {console.value(BUILD_PROJECT)}",
                                          delete_if_present(clients["codebuild"].delete_project,
                                                            name=BUILD_PROJECT))))
    if resource_exists(clients["iam"], "get_role", RoleName=BUILD_ROLE):
        plan.append((console.Markup(f"delete tagged IAM role {console.value(BUILD_ROLE)}"),
                     lambda: delete_role(clients["iam"], BUILD_ROLE)))
    if store_exists(clients["s3"], bucket):
        plan.append((console.Markup(f"delete tagged S3 bucket {console.value(bucket)} and the "
                                    "build uploads in it"),
                     lambda: delete_build_bucket(clients["s3"], bucket)))
    if find_log_group(clients["logs"], BUILD_LOG_GROUP) is not None:
        plan.append((console.Markup(f"delete tagged log group {console.value(BUILD_LOG_GROUP)}"),
                     lambda: delete_log_group(clients["logs"], BUILD_LOG_GROUP)))
    return plan


def delete_build_bucket(s3, name: str) -> None:
    if not store_exists(s3, name):
        return
    if not has_managed_tag(s3.get_bucket_tagging(Bucket=name).get("TagSet", []), "Key", "Value"):
        return
    while True:
        objects = s3.list_objects_v2(Bucket=name).get("Contents", [])
        if not objects:
            break
        s3.delete_objects(Bucket=name, Delete={"Objects": [{"Key": item["Key"]}
                                                           for item in objects]})
    s3.delete_bucket(Bucket=name)


def cost_estimate_for(logs, names: dict[str, str], region: str, cron: str,
                      schedule_exists: bool, usage: tuple[int, int] | None,
                      local_currency: str) -> CostEstimate:
    try:
        runs = config.runs_per_month(cron)
        recent = None
        if schedule_exists:
            with console.waiting(f"reading: past runs in CloudWatch log group "
                                 f"{console.value(names['log_group'])}"):
                recent = recent_stream_seconds(logs, names["log_group"])
        seconds, basis = run_basis(recent)
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
        fail(f"could not calculate the required monthly cost estimate: "
             f"{console.escape(str(exc))}")
    return cost_estimate(
        region, items,
        "excludes EventBridge Scheduler free tier, ECR storage, and CloudWatch Logs usage",
        local_currency)


def batch_clients(session) -> dict:
    return clients_for(session, "batch", "codebuild", "ec2", "ecr", "ecs")


def secrets(app: dict, action: str, assume_yes: bool, name: str | None = None) -> int:
    console.status("Checking your AWS sign-in...")
    session = ensure_session(app)
    expected_account, region = aws_settings(app, session)
    clients = batch_clients(session)
    _account, wait_for_permissions = preflight(
        clients["sts"], clients["iam"], expected_account, region, DEPLOYER_ACTIONS)
    wait_for_permissions()
    client = clients["secretsmanager"]
    name = resource_names(app["name"])["secret"]
    current = None
    if resource_exists(client, "describe_secret", SecretId=name):
        current = client.get_secret_value(SecretId=name).get("SecretString", "")

    def write(values: dict[str, str]) -> None:
        console.step(f"updating secret {console.value(name)}")
        ensure_secret(client, name, json.dumps(values, sort_keys=True))

    return run_secrets(action, app, current, write, assume_yes, name)


def deploy(app: dict, assume_yes: bool, plan_only: bool = False) -> int:
    cloud = build_in_cloud(app, "CodeBuild", DOCKER_PLATFORM)
    console.status("Checking your AWS sign-in...")
    session = ensure_session(app)
    expected_account, region = aws_settings(app, session)
    clients = batch_clients(session)
    account, wait_for_permissions = preflight(
        clients["sts"], clients["iam"], expected_account, region, DEPLOYER_ACTIONS)
    names = resource_names(app["name"])
    cron = config.cron_expression(app["schedule"])
    expression = aws_schedule_expression(cron)
    secrets = gather_secrets(app)
    payload = json.dumps(secrets, sort_keys=True)
    image = f"{account}.dkr.ecr.{region}.amazonaws.com/{REPOSITORY}:{names['image_tag']}"
    bucket = store_name(account)
    store = deployer_store(app, session, account) if app["storage"] else None

    console.status(f"Checking current state in account {console.value(account)} "
                   f"({console.value(region)})...")
    with console.waiting("reading: resources"):
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
        uploads = build_bucket(account, region)
        revisions = [item for item in active_job_definitions(clients["batch"], names["job_definition"])
                     if managed(item)]
        recorded = (revisions[0].get("tags") or {}) if revisions else {}
        deployed = recorded.get(BUILD_TAG)
        if cloud:
            project = build_project(clients["codebuild"])
            build_role_exists = resource_exists(clients["iam"], "get_role", RoleName=BUILD_ROLE)
            uploads_present = store_exists(clients["s3"], uploads)
            build_log_present = find_log_group(clients["logs"], BUILD_LOG_GROUP) is not None
    actions = [
        console.Markup(f"reconcile shared ECR repository {console.value(REPOSITORY)}, Batch "
                       f"compute environment {console.value(COMPUTE_ENVIRONMENT.name)}, and job "
                       f"queue {console.value(JOB_QUEUE.name)}"),
        image_action(app, console.Markup(f"build and push Docker image {console.value(image)} "
                                         f"({DOCKER_PLATFORM})"), "CodeBuild" if cloud else ""),
        console.Markup(("update" if secret_exists else "create")
                       + f" Secrets Manager secret {console.value(names['secret'])} "
                       f"({secret_contents(secrets)})"),
        "reconcile the execution, job, and scheduler IAM roles",
        console.Markup(f"allow {console.value(names['job_role'])} to update its own secret "
                       f"{console.value(names['secret'])}"),
        console.Markup(f"reconcile Batch job definition {console.value(names['job_definition'])} "
                       f"({float(JOB_VCPU):g} vCPU, {JOB_MEMORY} MiB, no time limit)"),
        console.Markup(("update" if schedule_exists else "create")
                       + f" EventBridge schedule {console.value(names['schedule'])}: "
                       f"{console.value(expression)} ({console.value(app['timezone'])})"
                       + (" (paused)" if app["pause"] else "")),
        console.Markup((f"use security group {console.value(SECURITY_GROUP)} "
                        f"({console.value(group_id)})" if group else
                        f"create security group {console.value(SECURITY_GROUP)} "
                        "(no inbound rules, all outbound)")
                       + f" in default VPC {console.value(vpc_id)}"),
        "run jobs in the default VPC subnets with a public IP",
    ]
    if cloud:
        actions += [
            console.Markup(("update" if project else "create")
                           + f" shared CodeBuild project {console.value(BUILD_PROJECT)} "
                           f"({console.value(BUILD_COMPUTE)}, {console.value(BUILD_IMAGE)})"),
            console.Markup(("update" if build_role_exists else "create")
                           + f" IAM role {console.value(BUILD_ROLE)} (push to ECR repository "
                           f"{console.value(REPOSITORY)}, read build uploads, write the build log)"),
            console.Markup(("use" if uploads_present else "create")
                           + f" private S3 bucket {console.value(uploads)} for build uploads "
                           "(each upload is deleted after its build, and expires after 1 day)"),
            console.Markup(("use" if build_log_present else "create")
                           + f" log group {console.value(BUILD_LOG_GROUP)} for the build log"),
        ]
    if move_environment:
        groups = ", ".join(console.value(group) for group in environment_groups)
        actions.append(console.Markup(
            f"move Batch compute environment {console.value(COMPUTE_ENVIRONMENT.name)} from "
            f"security group {groups} to {console.value(SECURITY_GROUP)}"))
    if store:
        actions += store_plan_lines(f"bucket {console.value(bucket)}", store_present,
                                    names["job_role"], app["name"])
    estimate = cost_estimate_for(clients["logs"], names, region, cron, schedule_exists, usage,
                                 regions.local_currency())
    wait_for_permissions()
    if plan_only:
        return show_plan(actions, estimate)
    if not confirm(actions, assume_yes, estimate):
        console.warn("Aborted; nothing was changed.")
        return DECLINED

    console.step(f"reconciling AWS resources in {console.value(account)} "
                 f"({console.value(region)})")
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
    image_digest = recorded.get("image-digest")

    def build(stage: Path) -> None:
        nonlocal image_digest
        if cloud:
            ensure_build_resources(clients, account, region, uploads, project)
            codebuild_build(stage, image, clients, uploads, app["name"])
        else:
            docker_build(stage, image, clients["ecr"])
        image_digest = pushed_digest(clients["ecr"], image)
        if image_digest is None:
            fail(f"ECR holds no image {console.value(image)} after the build")

    fingerprint = ensure_image(
        app, image, DOCKER_PLATFORM, "CodeBuild" if cloud else "", deployed,
        lambda: pushed_digest(clients["ecr"], image) == image_digest, build)
    console.step("reconciling job definition and schedule")
    desired = desired_job_definition(
        names, image, region, execution, job_role, secret_arn, environment)
    ensure_job_definition(clients["batch"], desired, image_digest, fingerprint)
    ensure_schedule(clients["scheduler"], names["schedule"], expression,
                    app["timezone"], scheduler_role, submit_job_target(names), app["pause"])
    console.done(f"Deployed {console.value(app['name'])}.")
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
        plan.append((console.Markup(f"deregister {len(arns)} tagged ECS task definition(s) in "
                                    f"family {console.value(family)} (older Fargate deployment)"),
                     lambda: deregister_task_definitions(ecs, arns)))
    if find_log_group(logs, names["legacy_log_group"]) is not None:
        plan.append((console.Markup(f"delete tagged log group "
                                    f"{console.value(names['legacy_log_group'])} "
                                    "(older Fargate deployment)"),
                     lambda: delete_log_group(logs, names["legacy_log_group"])))
    if resource_exists(iam, "get_role", RoleName=names["legacy_task_role"]):
        plan.append((console.Markup(f"delete tagged IAM role "
                                    f"{console.value(names['legacy_task_role'])} "
                                    "(older Fargate deployment)"),
                     lambda: delete_role(iam, names["legacy_task_role"])))
    if cluster_unused_after(ecs, family):
        plan.append((console.Markup(f"delete ECS cluster {console.value(LEGACY_CLUSTER)} "
                                    "(older Fargate deployment, no other apps use it)"),
                     lambda: note_if_gone(f"ECS cluster {console.value(LEGACY_CLUSTER)}",
                                          delete_if_present(
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
    account, wait_for_permissions = preflight(
        clients["sts"], clients["iam"], expected_account, region, DEPLOYER_ACTIONS)
    names = resource_names(app["name"])
    batch, scheduler, iam = clients["batch"], clients["scheduler"], clients["iam"]
    plan: list[tuple[str, Callable]] = []
    with console.waiting("reading: resources"):
        if resource_exists(scheduler, "get_schedule",
                           Name=names["schedule"], GroupName=SCHEDULE_GROUP):
            plan.append((console.Markup(f"delete EventBridge schedule "
                                        f"{console.value(names['schedule'])}"),
                         lambda: scheduler.delete_schedule(
                             Name=names["schedule"], GroupName=SCHEDULE_GROUP)))
        revisions = [revision for revision in active_job_definitions(batch, names["job_definition"])
                     if managed(revision)]
        if revisions:
            plan.append((console.Markup(f"deregister {len(revisions)} tagged revision(s) of Batch "
                                        f"job definition {console.value(names['job_definition'])}"),
                         lambda: deregister_job_definitions(batch, revisions)))
        if resource_exists(clients["secretsmanager"], "describe_secret", SecretId=names["secret"]):
            plan.append((console.Markup(f"delete secret {console.value(names['secret'])}"),
                         lambda: delete_secret(clients["secretsmanager"], names["secret"])))
        plan += [
            (console.Markup(f"delete tagged log group {console.value(names['log_group'])}"),
             lambda: delete_log_group(clients["logs"], names["log_group"])),
            ("delete tagged per-app IAM roles",
             lambda: [delete_role(iam, role) for role in
                      (names["scheduler_role"], names["job_role"], names["execution_role"])]),
            (console.Markup(f"delete image tag {console.value(names['image_tag'])} from ECR "
                            f"repository {console.value(REPOSITORY)}"),
             lambda: note_if_gone(
                 f"image tag {console.value(names['image_tag'])} in ECR repository "
                 f"{console.value(REPOSITORY)}",
                 delete_if_present(clients["ecr"].batch_delete_image, repositoryName=REPOSITORY,
                                   imageIds=[{"imageTag": names["image_tag"]}]))),
        ]
        plan += legacy_fargate_cleanup(clients["ecs"], clients["logs"], iam, names)
        if other_schedules(scheduler, names["schedule"]) == []:
            plan.append((console.Markup(f"delete schedule group {console.value(SCHEDULE_GROUP)} "
                                        "(no other apps use it)"),
                         lambda: note_if_gone(f"schedule group {console.value(SCHEDULE_GROUP)}",
                                              delete_schedule_group(scheduler))))
        if not other_job_definitions(batch, names["job_definition"]):
            for resource in shared_present(batch):
                plan.append((console.Markup(f"delete Batch {resource.label} "
                                            f"{console.value(resource.name)} (no other apps use it)"),
                             lambda resource=resource: note_if_gone(
                                 f"Batch {resource.label} {console.value(resource.name)}",
                                 remove(batch, resource))))
            vpc_id = default_vpc(clients["ec2"])
            group = find_security_group(clients["ec2"], vpc_id) if vpc_id else None
            if managed_group(group):
                plan.append((console.Markup(f"delete security group {console.value(SECURITY_GROUP)} "
                                            f"({console.value(group['GroupId'])}) "
                                            "(no other apps use it)"),
                             lambda: note_if_gone(f"security group {console.value(SECURITY_GROUP)}",
                                                  delete_security_group(clients["ec2"], group["GroupId"]))))
        repository_unused = repository_unused_after(clients["ecr"], names["image_tag"])
        # With the repository already gone, an earlier destroy stopped before the build resources.
        if repository_unused or not resource_exists(
                clients["ecr"], "describe_repositories", repositoryNames=[REPOSITORY]):
            plan += build_cleanup(clients, build_bucket(account, region))
        if repository_unused:
            plan.append((console.Markup(f"delete ECR repository {console.value(REPOSITORY)} "
                                        "(no other apps use it)"),
                         lambda: note_if_gone(f"ECR repository {console.value(REPOSITORY)}",
                                              delete_if_present(
                             clients["ecr"].delete_repository, repositoryName=REPOSITORY,
                             force=True))))
        bucket = store_name(account)
        store = deployer_store(app, session, account) if app["storage"] else None
        store_present = store_exists(clients["s3"], bucket) if store else False
        if store_present:
            warn_if_locked(store, app["name"])
    wait_for_permissions()
    if not confirm([line for line, _action in plan], assume_yes):
        console.warn("Aborted; nothing was changed.")
        return DECLINED

    for _line, action in plan:
        action()
    console.done(f"Removed {console.value(app['name'])} from account {console.value(account)} "
                 f"({console.value(region)}).")
    if store_present:
        console.say(store_kept_line(f"bucket {console.value(bucket)}", store.usage()[0],
                                    app["name"]))
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


def log_streams(batch, found: list[runs_cli.Run]) -> list[str]:
    ids = [run.id for run in found]
    streams = {}
    for start in range(0, len(ids), 100):
        for job in batch.describe_jobs(jobs=ids[start:start + 100]).get("jobs", []):
            streams[job["jobId"]] = (job.get("container") or {}).get("logStreamName") or "-"
    return [streams.get(run_id, "-") for run_id in ids]


def read_lines(batch, logs, log_group: str, run: runs_cli.Run,
               since: datetime | None = None) -> list[runs_cli.Line]:
    stream = log_stream(batch, run)
    if stream is None:
        return []
    lines = []
    token = None
    while True:
        kwargs = {"logGroupName": log_group, "logStreamName": stream, "startFromHead": True}
        if since is not None:
            kwargs["startTime"] = int(since.timestamp() * 1000)
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


def session_events(events) -> Iterator[dict]:
    """A live tail's events; the session's 3-hour limit ends it normally."""
    try:
        yield from events
    except ClientError as exc:
        if error_code(exc) != "SessionTimeoutException":
            raise runs_cli.StreamError(str(exc)) from exc
    except BotoCoreError as exc:
        raise runs_cli.StreamError(str(exc)) from exc


def tail_lines(updates: Iterator[dict]) -> Iterator[runs_cli.Line]:
    heard = time.monotonic()
    for update in updates:
        results = update.get("sessionUpdate", {}).get("sessionResults", [])
        for result in results:
            yield runs_cli.parse_line(result["message"], ms_to_utc(result["timestamp"]))
        if results:
            heard = time.monotonic()
        elif time.monotonic() - heard >= runs_cli.STREAM_QUIET:
            return


def stream_lines(batch, logs, log_group: str, run: runs_cli.Run,
                 follow: bool) -> Iterator[runs_cli.Line]:
    """With `follow`, the store's lines and then each line of a live tail."""
    if not follow:
        yield from read_lines(batch, logs, log_group, run)
        return
    group = find_log_group(logs, log_group)
    stream = log_stream(batch, run)
    if group is None or stream is None:
        return
    try:
        # CloudWatch creates the stream at the container's first line, and a live tail by
        # name fails until then. The prefix is the whole unique name, so it matches only it.
        events = logs.start_live_tail(logGroupIdentifiers=[group["logGroupArn"]],
                                      logStreamNamePrefixes=[stream])["responseStream"]
    except (BotoCoreError, ClientError) as exc:
        if error_code(exc) == "AccessDeniedException":
            raise runs_cli.StreamError(f"your AWS login may not perform logs:StartLiveTail on "
                                       f"log group {log_group}: {exc}") from exc
        raise runs_cli.StreamError(str(exc)) from exc
    try:
        updates = session_events(events)
        # The tail sends only what arrives after its sessionStart, so the store read waits for it.
        next(updates, None)
        yield from runs_cli.store_and_tail(
            lambda since: read_lines(batch, logs, log_group, run, since), tail_lines(updates), HOLD)
    finally:
        events.close()


def runs(app: dict, session, rest: list[str]) -> int:
    names = resource_names(app["name"])
    batch = session.client("batch", config=RETRIES)
    region = session.region_name
    return runs_cli.runs(lambda: list_runs(batch, names["job_definition"]), app["name"], rest,
                         page=console_url(session, f"https://{region}.console.aws.amazon.com"
                                                   f"/batch/home?region={region}#jobs"),
                         note=" ".join(filter(None, (
                             console_note(app, session),
                             f"Choose the job queue {console.value(JOB_QUEUE.name)} to see "
                             f"the runs of {console.value(names['job_definition'])}."))))


def current_schedule(scheduler, name: str, app_name: str) -> dict:
    try:
        return scheduler.get_schedule(Name=name, GroupName=SCHEDULE_GROUP)
    except Exception as exc:
        if not not_found(exc):
            raise
        fail(f"{console.value(app_name)} has no EventBridge schedule {console.value(name)}; "
             f"run {console.value(f'pdt deploy {app_name}')} first")


# What update_schedule needs back from get_schedule to change one field.
SCHEDULE_FIELDS = ("Name", "GroupName", "ScheduleExpression", "ScheduleExpressionTimezone",
                   "FlexibleTimeWindow", "Target", "Description")


def pause(app: dict, session, paused: bool) -> int:
    scheduler = session.client("scheduler", config=RETRIES)
    names = resource_names(app["name"])
    schedule = current_schedule(scheduler, names["schedule"], app["name"])
    state = "DISABLED" if paused else "ENABLED"
    if schedule.get("State") != state:
        scheduler.update_schedule(
            **{key: schedule[key] for key in SCHEDULE_FIELDS if key in schedule}, State=state)
    console.done(f"{'Paused' if paused else 'Unpaused'} {console.value(app['name'])}: "
                 f"EventBridge schedule {console.value(names['schedule'])} is {state}.")
    return 0


def start(app: dict, session, follow: bool = False) -> int:
    """Submit the job the schedule would submit, with the same input."""
    scheduler, batch = session.client("scheduler", config=RETRIES), session.client("batch", config=RETRIES)
    names = resource_names(app["name"])
    target = current_schedule(scheduler, names["schedule"], app["name"])["Target"]
    request = json.loads(target["Input"])
    response = batch.submit_job(
        jobName=request["JobName"], jobQueue=request["JobQueue"],
        jobDefinition=request["JobDefinition"], tags=dict(MANAGED_TAGS),
        containerOverrides={"environment": [{"name": lifecycle.TRIGGER_ENV, "value": "manual"}]})
    console.done(f"Started {console.value(app['name'])}: Batch job "
                 f"{console.value(response['jobId'])}.")
    if not follow:
        return 0
    return logs(app, session, ["--follow"], new=lambda run: run.id == response["jobId"])


def logs(app: dict, session, rest: list[str],
         new: Callable[[runs_cli.Run], bool] | None = None) -> int:
    names = resource_names(app["name"])
    batch, logs_client = session.client("batch", config=RETRIES), session.client("logs", config=RETRIES)
    return runs_cli.logs(
        lambda: list_runs(batch, names["job_definition"]),
        lambda run, since: read_lines(batch, logs_client, names["log_group"], run, since),
        app["name"], rest, store="CloudWatch Logs", delay=runs_cli.LOG_DELAY, hold=HOLD,
        stream=lambda run, follow: stream_lines(batch, logs_client, names["log_group"], run, follow),
        new=new, place=runs_cli.LogPlace(
            "CloudWatch log group", names["log_group"], "Log Stream",
            lambda runs: log_streams(batch, runs),
            lambda: console_url(session, log_group_url(session.region_name, names["log_group"])),
            lambda stream: console_url(
                session, log_group_url(session.region_name, names["log_group"], stream)),
            note=console_note(app, session)))
