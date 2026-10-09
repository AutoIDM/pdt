#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "boto3",
#     "pyyaml",
#     "rich",
#     "python-dotenv",
#     "backoff",
#     "fsspec",
#     "s3fs>=2024",
#     "duckdb",
#     "certifi",
# ]
# ///
"""Deploy an app to AWS.

Shared login, IAM, secret, log, and schedule code lives here. The job
itself, a scheduled AWS Batch job on Fargate, is in deploy_aws_batch.py.

The deploy itself talks to AWS through boto3. `pdt aws` and the SSO login
run AWS CLI v2 through `uvx`. Credentials live in ~/.aws.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pdt import config, console, storage_cli
from pdt.deploy_common import (
    STORE_PREFIX, STORE_TAGS, CostEstimate, convert_from_usd, fail, fetch_json, store_cost_label, store_name)
from pdt.utils import email_auth
from pdt.utils.storage import Store

AWS_CLI_V2 = "awscli @ https://github.com/aws/aws-cli/archive/refs/tags/2.36.49.tar.gz"
MANAGED_TAGS = {"managed-by": "pdt"}
SCHEDULE_GROUP = "pdt"
ASSUMED_RUN_MINUTES = 5.0
RECENT_RUNS = 3
PRICE_LIST_URL = "https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/{offer}/current/{region}/index.json"
ROLE_PROPAGATION_DELAYS = (1, 2, 4, 8)
COMMON_ACTIONS = [
    "iam:CreateRole",
    "iam:DeleteRole",
    "iam:DeleteRolePolicy",
    "iam:GetRole",
    "iam:ListRolePolicies",
    "iam:ListRoleTags",
    "iam:PassRole",
    "iam:PutRolePolicy",
    "iam:SimulatePrincipalPolicy",
    "iam:TagRole",
    "iam:UpdateAssumeRolePolicy",
    "logs:CreateLogGroup",
    "logs:DeleteLogGroup",
    "logs:DescribeLogGroups",
    "logs:DescribeLogStreams",
    "logs:FilterLogEvents",
    "logs:GetLogEvents",
    "logs:ListTagsForResource",
    "logs:PutRetentionPolicy",
    "logs:TagResource",
    "scheduler:CreateSchedule",
    "scheduler:CreateScheduleGroup",
    "scheduler:DeleteSchedule",
    "scheduler:DeleteScheduleGroup",
    "scheduler:GetSchedule",
    "scheduler:GetScheduleGroup",
    "scheduler:ListSchedules",
    "scheduler:TagResource",
    "scheduler:UpdateSchedule",
    "secretsmanager:CreateSecret",
    "secretsmanager:DeleteSecret",
    "secretsmanager:DescribeSecret",
    "secretsmanager:GetSecretValue",
    "secretsmanager:PutSecretValue",
    "secretsmanager:RestoreSecret",
    "secretsmanager:TagResource",
    "s3:CreateBucket",
    "s3:GetBucketTagging",
    "s3:GetObject",
    "s3:ListBucket",
    "s3:PutBucketPublicAccessBlock",
    "s3:PutBucketTagging",
]

ROLE_ARN = "arn:aws:iam::{account}:role/pdt-*"
LOG_GROUP_ARNS = [
    "arn:aws:logs:{region}:{account}:log-group:/pdt/*",
    "arn:aws:logs:{region}:{account}:log-group:/pdt/*:*",
    # The /ecs/pdt-<app> name of a Fargate deployment, which destroy still removes.
    "arn:aws:logs:{region}:{account}:log-group:/ecs/pdt-*",
    "arn:aws:logs:{region}:{account}:log-group:/ecs/pdt-*:*",
]
BATCH_ARNS = {
    "compute_environment": "arn:aws:batch:{region}:{account}:compute-environment/pdt",
    "job_queue": "arn:aws:batch:{region}:{account}:job-queue/pdt",
    "job_definition": "arn:aws:batch:{region}:{account}:job-definition/pdt-*",
    "job_definition_revision": "arn:aws:batch:{region}:{account}:job-definition/pdt-*:*",
}
SECURITY_GROUP_ARN = "arn:aws:ec2:{region}:{account}:security-group/*"
# Each action pdt calls, grouped with the ARNs of the pdt resources it touches.
# deployer_policy prints these and preflight simulates them, so both agree.
POLICY_SCOPES = [
    {"Action": ["iam:CreateRole", "iam:DeleteRole", "iam:DeleteRolePolicy", "iam:GetRole",
                "iam:ListRolePolicies", "iam:ListRoleTags", "iam:PutRolePolicy",
                "iam:TagRole", "iam:UpdateAssumeRolePolicy"],
     "Resource": [ROLE_ARN]},
    # The job definition hands the execution and job roles to Batch, which runs them
    # as ECS tasks; the schedule hands its role to the scheduler.
    {"Action": ["iam:PassRole"], "Resource": [ROLE_ARN],
     "Condition": {"StringEquals": {"iam:PassedToService": [
         "batch.amazonaws.com", "ecs-tasks.amazonaws.com", "scheduler.amazonaws.com"]}}},
    # Creating the compute environment creates Batch's service-linked role on first use.
    {"Action": ["iam:CreateServiceLinkedRole"],
     "Resource": ["arn:aws:iam::{account}:role/aws-service-role/batch.amazonaws.com/*"],
     "Condition": {"StringEquals": {"iam:AWSServiceName": ["batch.amazonaws.com"]}}},
    # preflight checks the operator's own login, which has no pdt name.
    {"Action": ["iam:SimulatePrincipalPolicy"],
     "Resource": ["arn:aws:iam::{account}:user/*", "arn:aws:iam::{account}:role/*"]},
    {"Action": ["logs:CreateLogGroup", "logs:DeleteLogGroup", "logs:DescribeLogStreams",
                "logs:FilterLogEvents", "logs:ListTagsForResource",
                "logs:PutRetentionPolicy", "logs:TagResource"],
     "Resource": LOG_GROUP_ARNS},
    # GetLogEvents reads a log stream, which only the ":*" group ARNs cover.
    {"Action": ["logs:GetLogEvents"],
     "Resource": [arn for arn in LOG_GROUP_ARNS if arn.endswith(":*")]},
    {"Action": ["scheduler:CreateSchedule", "scheduler:DeleteSchedule",
                "scheduler:GetSchedule", "scheduler:UpdateSchedule"],
     "Resource": [f"arn:aws:scheduler:{{region}}:{{account}}:schedule/{SCHEDULE_GROUP}/*"]},
    {"Action": ["scheduler:CreateScheduleGroup", "scheduler:DeleteScheduleGroup",
                "scheduler:GetScheduleGroup", "scheduler:TagResource"],
     "Resource": [f"arn:aws:scheduler:{{region}}:{{account}}:schedule-group/{SCHEDULE_GROUP}"]},
    {"Action": ["secretsmanager:CreateSecret", "secretsmanager:DeleteSecret",
                "secretsmanager:DescribeSecret", "secretsmanager:GetSecretValue",
                "secretsmanager:PutSecretValue", "secretsmanager:RestoreSecret",
                "secretsmanager:TagResource"],
     "Resource": ["arn:aws:secretsmanager:{region}:{account}:secret:pdt-*"]},
    {"Action": ["s3:CreateBucket", "s3:GetBucketTagging", "s3:ListBucket",
                "s3:PutBucketPublicAccessBlock", "s3:PutBucketTagging"],
     "Resource": [f"arn:aws:s3:::{STORE_PREFIX}-*"]},
    {"Action": ["s3:GetObject"], "Resource": [f"arn:aws:s3:::{STORE_PREFIX}-*/*"]},
    {"Action": ["ecr:BatchCheckLayerAvailability", "ecr:BatchDeleteImage",
                "ecr:CompleteLayerUpload", "ecr:CreateRepository", "ecr:DeleteRepository",
                "ecr:DescribeImages", "ecr:DescribeRepositories", "ecr:InitiateLayerUpload",
                "ecr:ListImages", "ecr:PutImage", "ecr:TagResource", "ecr:UploadLayerPart"],
     "Resource": ["arn:aws:ecr:{region}:{account}:repository/pdt"]},
    {"Action": ["batch:CreateComputeEnvironment", "batch:DeleteComputeEnvironment",
                "batch:UpdateComputeEnvironment"],
     "Resource": [BATCH_ARNS["compute_environment"]]},
    # A job queue names the compute environment it feeds, so its calls touch both.
    {"Action": ["batch:CreateJobQueue", "batch:UpdateJobQueue"],
     "Resource": [BATCH_ARNS["job_queue"], BATCH_ARNS["compute_environment"]]},
    {"Action": ["batch:DeleteJobQueue"], "Resource": [BATCH_ARNS["job_queue"]]},
    {"Action": ["batch:DeregisterJobDefinition", "batch:RegisterJobDefinition"],
     "Resource": [BATCH_ARNS["job_definition"], BATCH_ARNS["job_definition_revision"]]},
    # pdt start submits the job the schedule would submit, so it names both.
    {"Action": ["batch:SubmitJob"],
     "Resource": [BATCH_ARNS["job_definition"], BATCH_ARNS["job_definition_revision"],
                  BATCH_ARNS["job_queue"]]},
    {"Action": ["batch:TagResource"], "Resource": list(BATCH_ARNS.values())},
    # Destroy still clears the cluster a Fargate deployment of the same app left behind.
    {"Action": ["ecs:DeleteCluster", "ecs:DescribeClusters"],
     "Resource": ["arn:aws:ecs:{region}:{account}:cluster/pdt"]},
    {"Action": ["ecs:ListTagsForResource"],
     "Resource": ["arn:aws:ecs:{region}:{account}:cluster/pdt",
                  "arn:aws:ecs:{region}:{account}:task-definition/pdt-*:*"]},
    # A security group's ARN holds its id, not its name, so the managed-by tag scopes it.
    # CreateTags is allowed only inside CreateSecurityGroup, so no other group can gain the tag.
    {"Action": ["ec2:CreateSecurityGroup"], "Resource": ["arn:aws:ec2:{region}:{account}:vpc/*"]},
    {"Action": ["ec2:CreateSecurityGroup"], "Resource": [SECURITY_GROUP_ARN],
     "Condition": {"StringEquals": {"aws:RequestTag/managed-by": ["pdt"]}}},
    {"Action": ["ec2:CreateTags"], "Resource": [SECURITY_GROUP_ARN],
     "Condition": {"StringEquals": {"ec2:CreateAction": ["CreateSecurityGroup"]}}},
    {"Action": ["ec2:DeleteSecurityGroup"], "Resource": [SECURITY_GROUP_ARN],
     "Condition": {"StringEquals": {"aws:ResourceTag/managed-by": ["pdt"]}}},
    {"Action": [
        "batch:DescribeComputeEnvironments",  # Batch Describe and List calls take no resource ARN.
        "batch:DescribeJobDefinitions",
        "batch:DescribeJobQueues",
        "batch:DescribeJobs",
        "batch:ListJobs",
        "ec2:DescribeSecurityGroups",  # EC2 Describe calls take no resource ARN.
        "ec2:DescribeSubnets",
        "ec2:DescribeVpcs",
        "ecr:GetAuthorizationToken",  # AWS allows only "*".
        "ecs:DeregisterTaskDefinition",  # AWS allows only "*".
        "ecs:ListTaskDefinitionFamilies",  # List calls take no resource ARN.
        "ecs:ListTaskDefinitions",
        "ecs:ListTasks",  # Scoped by container instance, not cluster; kept on "*" to be safe.
        "logs:DescribeLogGroups",  # Unsure it honors a log group ARN; kept on "*" to be safe.
        "scheduler:ListSchedules",  # List calls take no resource ARN.
     ],
     "Resource": ["*"]},
]


def error_code(exc: Exception) -> str:
    return getattr(exc, "response", {}).get("Error", {}).get("Code", "")


def not_found(exc: Exception) -> bool:
    if error_code(exc) in {
        "NoSuchEntity", "ResourceNotFoundException", "ResourceNotFound",
        "ClusterNotFoundException", "RepositoryNotFoundException", "InvalidGroup.NotFound",
    }:
        return True
    # ECS and Batch report a missing task definition family or job queue as a
    # generic ClientException.
    message = getattr(exc, "response", {}).get("Error", {}).get("Message", "")
    return error_code(exc) == "ClientException" and (
        "Unable to describe task definition" in message or "does not exist" in message)


def delete_if_present(call, **kwargs) -> bool:
    """Run a delete call; False when AWS answers that the resource is already gone.

    A sibling app's destroy can remove a shared resource between our check and our delete.
    """
    try:
        call(**kwargs)
    except Exception as exc:
        if not not_found(exc):
            raise
        return False
    return True


def role_propagation_error(exc: Exception) -> bool:
    code = error_code(exc)
    response_message = getattr(exc, "response", {}).get("Error", {}).get("Message", "")
    message = f"{exc} {response_message}".lower()
    return (
        code in {"InvalidParameterValueException", "ValidationException",
                 "ClientException", "InvalidParameterException"}
        and "role" in message
        and any(fragment in message for fragment in (
            "cannot be assumed", "could not be assumed", "does not exist",
            "not valid", "unable to assume", "assume the role", "pass role",
        ))
    )


def retry(operation, retryable, reason: str, delays: tuple[int, ...], sleep=time.sleep):
    for delay in (*delays, None):
        try:
            return operation()
        except Exception as exc:
            if delay is None or not retryable(exc):
                raise
            console.bullet(f"{console.escape(reason)}; retrying in {delay}s...", indent=4)
            sleep(delay)
    raise AssertionError("unreachable")


def with_role_propagation_retry(operation, sleep=time.sleep):
    return retry(operation, role_propagation_error, "IAM role is not visible yet",
                 ROLE_PROPAGATION_DELAYS, sleep)


def adopt_account(app: dict, session) -> str:
    # AWS gives one account per credential set, so there is nothing to pick.
    identity = session.client("sts").get_caller_identity()
    account = identity["Account"]
    console.field("These credentials belong to AWS account", account)
    console.bullet(console.escape(identity["Arn"]))
    saved = config.save_platform_key(app, "account", account)
    console.done(f"Saved account {account} to {saved.relative_to(config.find_project())}.")
    return account


def aws_settings(app: dict, session) -> tuple[str, str]:
    platform = app["platform"]
    account = str(platform.get("account") or "").strip()
    if not account:
        account = adopt_account(app, session)
    region = str(platform.get("region") or session.region_name or
                 os.environ.get("AWS_DEFAULT_REGION") or "")
    if not region:
        fail("AWS region is required; set platform.region, AWS_REGION, or AWS_DEFAULT_REGION")
    return account, region


def iam_tags(extra: dict[str, str] | None = None) -> list[dict[str, str]]:
    return [{"Key": key, "Value": value}
            for key, value in {**MANAGED_TAGS, **(extra or {})}.items()]


def has_managed_tag(tags: list[dict], key_name: str, value_name: str) -> bool:
    return any(tag.get(key_name) == "managed-by" and tag.get(value_name) == "pdt"
               for tag in tags)


def deployer_policy(actions: list[str], account: str, region: str) -> dict:
    wanted = set(actions)
    statements = []
    for scope in POLICY_SCOPES:
        granted = [action for action in scope["Action"] if action in wanted]
        if not granted:
            continue
        statement = {"Effect": "Allow", "Action": granted, "Resource": [
            arn.format(account=account, region=region) for arn in scope["Resource"]]}
        if "Condition" in scope:
            statement["Condition"] = scope["Condition"]
        statements.append(statement)
    unscoped = wanted - {action for scope in POLICY_SCOPES for action in scope["Action"]}
    if unscoped:
        raise AssertionError(f"POLICY_SCOPES has no resource for {sorted(unscoped)}")
    return {"Version": "2012-10-17", "Statement": statements}


def simulations(policy: dict) -> list[dict]:
    """One SimulatePrincipalPolicy request per statement, with its ARNs and condition,
    so a login holding exactly the printed policy passes."""
    requests = []
    for statement in policy["Statement"]:
        request = {"ActionNames": statement["Action"]}
        if statement["Resource"] != ["*"]:
            request["ResourceArns"] = statement["Resource"]
        passed = statement.get("Condition", {}).get("StringEquals", {})
        requests += [
            {**request, "ContextEntries": [{
                "ContextKeyName": key, "ContextKeyValues": [value], "ContextKeyType": "string"}]}
            for key, values in passed.items() for value in values] or [request]
    return requests


def print_permission_help(identity: str, detail: str, actions: list[str],
                          account: str, region: str) -> None:
    console.warn("AWS blocked this deployment because the current login lacks a permission.")
    if detail:
        console.say(f"AWS said: {detail}")
    console.field("Current AWS login", identity)
    console.say("Send the policy below to the person who manages your AWS account.")
    console.say("Ask them to add it to this login, then run the same command again.")
    console.say(json.dumps(deployer_policy(actions, account, region), indent=2))


def principal_arn(identity_arn: str, account: str) -> str:
    marker = f"arn:aws:sts::{account}:assumed-role/"
    if identity_arn.startswith(marker):
        role_name = identity_arn[len(marker):].split("/", 1)[0]
        return f"arn:aws:iam::{account}:role/{role_name}"
    return identity_arn


def can_ask() -> bool:
    return email_auth.can_prompt(None)


def ask(prompt: str) -> str:
    try:
        return input(prompt).strip()
    except EOFError:
        return ""


def choose_profile(app: dict, session) -> str:
    profiles = session.available_profiles
    if not profiles:
        console.warn("No AWS credentials or profiles were found on this computer.")
        console.say("Create a profile first, then select it:")
        console.command("pdt aws configure sso", "or: pdt aws configure")
        console.command("profile: <profile-name>", "under platform: in pdt.yml")
        fail("run the same command again after you create a profile")
    if len(profiles) == 1:
        console.field("Using the only AWS profile on this computer", profiles[0])
        return profiles[0]
    if not can_ask():
        fail("no AWS profile selected; add `profile: <name>` under platform: in pdt.yml, "
             f"or set AWS_PROFILE=<name> (profiles: {', '.join(profiles)})")
    console.heading("No AWS profile is selected. Profiles on this computer:")
    for number, profile in enumerate(profiles, start=1):
        console.choice(number, profile)
    answer = ask(f"Which profile do you want to use? [1-{len(profiles)}] ")
    if answer.isdigit() and 1 <= int(answer) <= len(profiles):
        profile = profiles[int(answer) - 1]
    elif answer in profiles:
        profile = answer
    else:
        fail("no AWS profile selected; add `profile: <name>` under platform: in pdt.yml, "
             "or set AWS_PROFILE=<name>")
    saved = config.save_platform_key(app, "profile", profile)
    console.done(f"Saved profile {profile} to {saved.relative_to(config.find_project())}.")
    return profile


def aws_cli() -> list[str]:
    """Return the command that starts the pinned AWS CLI.

    PyPI has only version 1 of the AWS CLI. pdt uses version 2, so uvx
    installs it from a source archive on GitHub (AWS_CLI_V2).

    Without --offline, uvx sends a request to GitHub each time it starts,
    to check the archive. It does this also when the CLI is already in the
    uv cache. If the request fails, uvx stops, and the pdt command fails.

    To prevent this, the function first runs uvx with --offline and an empty
    Python command. This test passes only if the CLI is in the uv cache.
    - If the test passes, the command uses --offline. uvx starts the cached
      CLI and sends no request to GitHub.
    - If the test fails, the command does not use --offline. uvx downloads
      the archive and installs the CLI.
    """
    cached = subprocess.run(["uvx", "--offline", "--from", AWS_CLI_V2, "python", "-c", ""],
                            capture_output=True).returncode == 0
    if not cached:
        console.status("Installing the AWS CLI. This happens once and can take several minutes...")
    return ["uvx", *(["--offline"] if cached else []), "--from", AWS_CLI_V2, "aws"]


def sso_login(profile: str | None) -> bool:
    command = [*aws_cli(), "sso", "login"]
    shown = "pdt aws sso login"
    if profile:
        command += ["--profile", profile]
        shown += f" --profile {profile}"
    console.warn("Your AWS login has expired or is missing.")
    if not can_ask():
        console.say(f"Log in first: {shown}")
        return False
    answer = ask(f"Log in now with `{shown}` (opens a browser)? [y/N] ")
    if answer.lower() not in ("y", "yes"):
        return False
    return subprocess.run(command, check=False).returncode == 0


def relogin(app: dict) -> int:
    session = boto3.Session()
    name = app["platform"].get("profile") or os.environ.get("AWS_PROFILE") or ""
    if name not in session.available_profiles:
        name = choose_profile(app, session)
    console.status(f"Logging in to AWS profile {name}...")
    if subprocess.run([*aws_cli(), "sso", "login", "--profile", name]).returncode != 0:
        console.warn(f"If {name} uses access keys instead of SSO there is no login to "
                     f"refresh; run `pdt aws configure --profile {name}` to replace the keys.")
        fail("pdt aws sso login failed")
    identity = boto3.Session(profile_name=name).client("sts").get_caller_identity()
    console.done(f"Signed in as {identity['Arn']}")
    console.field("Account", identity["Account"])
    return 0


def login_error(exc: Exception) -> bool:
    name = type(exc).__name__
    return ("SSO" in name or "Token" in name
            or error_code(exc) in {"ExpiredToken", "ExpiredTokenException",
                                   "InvalidClientTokenId", "UnrecognizedClientException"})


def ensure_session(app: dict):
    region = app["platform"].get("region") or None
    profile = app["platform"].get("profile") or None
    if profile and profile not in boto3.Session().available_profiles:
        console.warn(f"AWS profile {profile!r} from your config is not on this computer.")
        profile = choose_profile(app, boto3.Session())
    session = boto3.Session(region_name=region, profile_name=profile)
    if session.get_credentials() is None:
        session = boto3.Session(region_name=region, profile_name=choose_profile(app, session))
    try:
        session.client("sts").get_caller_identity()
    except Exception as exc:  # noqa: BLE001 - credential providers raise several types
        if not login_error(exc) or not sso_login(session.profile_name):
            profile = session.profile_name or "<profile-name>"
            fail(f"AWS credentials are unavailable or invalid: {exc}\n"
                 f"Log in first (for example: pdt aws sso login --profile {profile}), "
                 f"then run the same command again.")
        session = boto3.Session(region_name=region, profile_name=session.profile_name)
    return session


def preflight(sts, iam, expected_account: str, region: str,
              actions: list[str]) -> tuple[str, str]:
    try:
        identity = sts.get_caller_identity()
    except Exception as exc:  # noqa: BLE001 - credential providers raise several types
        fail(f"AWS credentials are unavailable or invalid: {exc}")
    account = identity["Account"]
    identity_arn = identity["Arn"]
    if expected_account != account:
        fail(f"configured AWS account {expected_account} does not match credentials ({account})")
    source_arn = principal_arn(identity_arn, account)
    if source_arn.endswith(":root"):
        return account, identity_arn
    denied = set()
    try:
        for request in simulations(deployer_policy(actions, account, region)):
            result = iam.simulate_principal_policy(PolicySourceArn=source_arn, **request)
            denied |= {item["EvalActionName"] for item in result["EvaluationResults"]
                       if item["EvalDecision"] != "allowed"
                       or any(resource["EvalResourceDecision"] != "allowed"
                              for resource in item.get("ResourceSpecificResults", []))}
    except ClientError as exc:
        if error_code(exc) in {"AccessDenied", "AccessDeniedException"}:
            print_permission_help(identity_arn, str(exc), actions, account, region)
            raise SystemExit(1) from exc
        raise
    if denied:
        print_permission_help(identity_arn, "These required actions are not allowed: "
                              + ", ".join(sorted(denied)), actions, account, region)
        raise SystemExit(1)
    return account, identity_arn


def trust_policy(service: str) -> str:
    return json.dumps({
        "Version": "2012-10-17",
        "Statement": [{"Effect": "Allow", "Principal": {"Service": service},
                       "Action": "sts:AssumeRole"}],
    }, sort_keys=True)


def ensure_role(iam, name: str, service: str, policy_name: str,
                statements: list[dict]) -> str:
    try:
        role = iam.get_role(RoleName=name)["Role"]
        if not has_managed_tag(role.get("Tags", []), "Key", "Value"):
            fail(f"IAM role {name} exists but is not managed by PDT")
        iam.update_assume_role_policy(
            RoleName=name, PolicyDocument=trust_policy(service))
        iam.tag_role(RoleName=name, Tags=iam_tags())
    except Exception as exc:
        if not not_found(exc):
            raise
        role = iam.create_role(
            RoleName=name,
            AssumeRolePolicyDocument=trust_policy(service),
            Description="Managed by PDT",
            Tags=iam_tags(),
        )["Role"]
    for existing in iam.list_role_policies(RoleName=name).get("PolicyNames", []):
        if existing != policy_name or not statements:
            iam.delete_role_policy(RoleName=name, PolicyName=existing)
    if not statements:
        return role["Arn"]
    document = json.dumps({
        "Version": "2012-10-17", "Statement": statements,
    }, sort_keys=True)
    iam.put_role_policy(
        RoleName=name, PolicyName=policy_name, PolicyDocument=document)
    return role["Arn"]


def store_url(bucket: str, app_name: str) -> str:
    return f"s3://{bucket}/{app_name}/"


def secret_statements(secret_arn: str) -> list[dict]:
    """The task may read and update its own env secret, and nothing else."""
    return [{"Effect": "Allow",
             "Action": ["secretsmanager:GetSecretValue", "secretsmanager:PutSecretValue"],
             "Resource": secret_arn}]


def store_statements(bucket: str, app_name: str) -> list[dict]:
    return [
        {"Effect": "Allow",
         "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
         "Resource": f"arn:aws:s3:::{bucket}/{app_name}/*"},
        {"Effect": "Allow", "Action": ["s3:ListBucket"],
         "Resource": f"arn:aws:s3:::{bucket}",
         "Condition": {"StringLike": {"s3:prefix": [f"{app_name}/*"]}}},
    ]


def store_exists(s3, name: str) -> bool:
    try:
        s3.head_bucket(Bucket=name)
        return True
    except ClientError as exc:
        if error_code(exc) == "404":
            return False
        raise


def ensure_store(s3, name: str, region: str) -> None:
    if store_exists(s3, name):
        tags = s3.get_bucket_tagging(Bucket=name).get("TagSet", [])
        if not has_managed_tag(tags, "Key", "Value"):
            fail(f"S3 bucket {name} exists but is not managed by PDT")
        return
    location = {} if region == "us-east-1" else {
        "CreateBucketConfiguration": {"LocationConstraint": region}}
    s3.create_bucket(Bucket=name, **location)
    s3.put_public_access_block(Bucket=name, PublicAccessBlockConfiguration={
        "BlockPublicAcls": True, "IgnorePublicAcls": True,
        "BlockPublicPolicy": True, "RestrictPublicBuckets": True,
    })
    s3.put_bucket_tagging(Bucket=name, Tagging={
        "TagSet": [{"Key": key, "Value": value} for key, value in STORE_TAGS.items()]})


def store_cost(usage: tuple[int, int], region: str) -> tuple[str, float]:
    count, size = usage
    price = list_price("AmazonS3", region, "TimedStorage-ByteHrs", volumeType="Standard")
    return store_cost_label(count, size), size / 1024 ** 3 * price


def deployer_store(app: dict, session, account: str) -> Store:
    return Store(store_url(store_name(account), app["name"]), session)


def storage(app: dict, session, account: str, rest: list[str], assume_yes: bool) -> int:
    return storage_cli.run(deployer_store(app, session, account), app, rest, assume_yes)


def log_group_url(region: str, log_group: str) -> str:
    # The CloudWatch console double-encodes names in its URLs: "/" -> "%2F" -> "$252F".
    return (f"https://{region}.console.aws.amazon.com/cloudwatch/home?region={region}"
            f"#logsV2:log-groups/log-group/{log_group.replace('/', '$252F')}")


def find_log_group(logs, name: str) -> dict | None:
    # describe_log_groups matches a prefix, so the exact name is filtered here.
    groups = logs.describe_log_groups(logGroupNamePrefix=name).get("logGroups", [])
    return next((group for group in groups if group["logGroupName"] == name), None)


def ensure_log_group(logs, name: str) -> None:
    group = find_log_group(logs, name)
    if group is not None:
        arn = group.get("logGroupArn") or group["arn"].removesuffix(":*")
        tags = logs.list_tags_for_resource(resourceArn=arn).get("tags", {})
        if tags.get("managed-by") != "pdt":
            fail(f"CloudWatch log group {name} exists but is not managed by PDT")
    else:
        logs.create_log_group(logGroupName=name, tags=MANAGED_TAGS)
    logs.put_retention_policy(logGroupName=name, retentionInDays=30)


def ensure_secret(secrets, name: str, payload: str) -> str:
    try:
        current = secrets.describe_secret(SecretId=name)
        arn = current["ARN"]
        if not has_managed_tag(current.get("Tags", []), "Key", "Value"):
            fail(f"Secrets Manager secret {name} exists but is not managed by PDT")
        if current.get("DeletedDate"):
            secrets.restore_secret(SecretId=name)
        try:
            value = secrets.get_secret_value(SecretId=name).get("SecretString", "")
        except Exception:  # noqa: BLE001 - a missing or inaccessible value is replaced
            value = None
        if value != payload:
            secrets.put_secret_value(SecretId=name, SecretString=payload)
        secrets.tag_resource(SecretId=arn, Tags=iam_tags())
        return arn
    except Exception as exc:
        if not not_found(exc):
            raise
    return secrets.create_secret(
        Name=name,
        SecretString=payload,
        Tags=iam_tags(),
        Description="PDT_ENV_JSON for a pdt job",
    )["ARN"]


def list_price(offer: str, region: str, usagetype_suffix: str, **attributes: str) -> float:
    url = PRICE_LIST_URL.format(offer=offer, region=region)
    data = fetch_json(url, timeout=60)
    for sku, product in data["products"].items():
        if not product["attributes"].get("usagetype", "").endswith(usagetype_suffix):
            continue
        if any(product["attributes"].get(key) != value for key, value in attributes.items()):
            continue
        for term in data["terms"]["OnDemand"].get(sku, {}).values():
            for dimension in term["priceDimensions"].values():
                if dimension.get("beginRange", "0") == "0":
                    return float(dimension["pricePerUnit"]["USD"])
    raise LookupError(f"no {usagetype_suffix!r} price for {offer} in region {region}")


def recent_stream_seconds(logs, log_group: str) -> float | None:
    # One log stream per run (Batch job); its first and last event bound the run.
    try:
        streams = logs.describe_log_streams(
            logGroupName=log_group, orderBy="LastEventTime", descending=True,
            limit=RECENT_RUNS).get("logStreams", [])
    except ClientError as exc:
        if not_found(exc):
            return None
        raise
    durations = [(s["lastEventTimestamp"] - s["firstEventTimestamp"]) / 1000
                 for s in streams if "firstEventTimestamp" in s and "lastEventTimestamp" in s]
    if not durations:
        return None
    return sum(durations) / len(durations)


def cost_estimate(region: str, items: list[tuple[str, float]],
                  excludes: str, local_currency: str) -> CostEstimate:
    # The AWS price list holds USD only and AWS publishes no exchange rate.
    items, currency, converted = convert_from_usd(items, local_currency)
    return CostEstimate(items, f"{region} list prices{converted}, before free tiers", excludes,
                        currency)


def run_basis(seconds: float | None) -> tuple[float, str]:
    if seconds is None:
        return ASSUMED_RUN_MINUTES * 60, f"{ASSUMED_RUN_MINUTES:g} min assumed"
    return seconds, f"{seconds / 60:.1f} min avg of recent runs"


def aws_schedule_expression(cron: str) -> str:
    minute, hour, dom, month, dow = cron.split()
    if dom != "*" and dow != "*":
        raise config.ConfigError(
            "AWS schedules cannot restrict both day-of-month and day-of-week")
    if dow != "*":
        names = ["SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]
        parts: list[str] = []
        for part in dow.split(","):
            span, slash, step = part.partition("/")
            if "-" in span:
                start, end = span.split("-", 1)
                if start.isdigit() and end.isdigit():
                    if not (0 <= int(start) <= 7 and 0 <= int(end) <= 7):
                        raise config.ConfigError(
                            f"AWS day-of-week values must be between 0 and 7: {dow}")
                    span = f"{names[int(start)]}-{names[int(end)]}"
            elif span.isdigit():
                if not 0 <= int(span) <= 7:
                    raise config.ConfigError(
                        f"AWS day-of-week values must be between 0 and 7: {dow}")
                span = names[int(span)]
            parts.append(span + (slash + step if slash else ""))
        dow = ",".join(parts)
    if dom == "*":
        dom = "?"
    else:
        dow = "?"
    return f"cron({minute} {hour} {dom} {month} {dow} *)"


def ensure_schedule_group(scheduler) -> None:
    try:
        scheduler.get_schedule_group(Name=SCHEDULE_GROUP)
    except Exception as exc:
        if not not_found(exc):
            raise
        scheduler.create_schedule_group(Name=SCHEDULE_GROUP, Tags=iam_tags())


def ensure_schedule(scheduler, name: str, expression: str, timezone: str,
                    role_arn: str, target: dict, paused: bool = False) -> None:
    # Scheduler tags live on groups, not schedules: membership in the
    # tagged pdt group is the ownership marker.
    ensure_schedule_group(scheduler)
    request = {
        "Name": name,
        "GroupName": SCHEDULE_GROUP,
        "ScheduleExpression": expression,
        "ScheduleExpressionTimezone": timezone,
        "FlexibleTimeWindow": {"Mode": "OFF"},
        "State": "DISABLED" if paused else "ENABLED",
        "Target": {
            **target,
            "RoleArn": role_arn,
            "RetryPolicy": {"MaximumRetryAttempts": 1},
        },
    }
    try:
        scheduler.get_schedule(Name=name, GroupName=SCHEDULE_GROUP)
        with_role_propagation_retry(lambda: scheduler.update_schedule(**request))
    except Exception as exc:
        if not not_found(exc):
            raise
        with_role_propagation_retry(lambda: scheduler.create_schedule(
            **request, Description="Managed by PDT", ActionAfterCompletion="NONE"))


def resource_exists(client, operation: str, **kwargs) -> bool:
    try:
        getattr(client, operation)(**kwargs)
        return True
    except Exception as exc:
        if not not_found(exc):
            raise
        return False


def clients_for(session) -> dict:
    return {name: session.client(name) for name in
            ("sts", "logs", "secretsmanager", "iam", "scheduler", "s3")}


def delete_secret(secrets, name: str) -> None:
    try:
        current = secrets.describe_secret(SecretId=name)
    except Exception as exc:
        if not not_found(exc):
            raise
        return
    if has_managed_tag(current.get("Tags", []), "Key", "Value"):
        secrets.delete_secret(SecretId=name, ForceDeleteWithoutRecovery=True)


def delete_log_group(logs, name: str) -> None:
    group = find_log_group(logs, name)
    if group is None:
        return
    arn = group.get("logGroupArn") or group["arn"].removesuffix(":*")
    tags = logs.list_tags_for_resource(resourceArn=arn).get("tags", {})
    if tags.get("managed-by") == "pdt":
        delete_if_present(logs.delete_log_group, logGroupName=name)


def other_schedules(scheduler, name: str) -> list[str] | None:
    """Names of other schedules in the pdt group; None when the group is absent."""
    try:
        schedules = scheduler.list_schedules(GroupName=SCHEDULE_GROUP).get("Schedules", [])
    except Exception as exc:
        if not not_found(exc):
            raise
        return None
    return [item["Name"] for item in schedules if item["Name"] != name]


def delete_schedule_group(scheduler) -> bool:
    return delete_if_present(scheduler.delete_schedule_group, Name=SCHEDULE_GROUP)


def delete_role(iam, name: str) -> None:
    try:
        role = iam.get_role(RoleName=name)["Role"]
        if not has_managed_tag(role.get("Tags", []), "Key", "Value"):
            return
        for policy in iam.list_role_policies(RoleName=name).get("PolicyNames", []):
            iam.delete_role_policy(RoleName=name, PolicyName=policy)
        iam.delete_role(RoleName=name)
    except Exception as exc:
        if not not_found(exc):
            raise


def load_app(app_name: str) -> dict:
    try:
        app = config.merged_app(app_name)
    except config.ConfigError as exc:
        fail(str(exc))
    config.load_env(app["dir"])
    return app


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "aws":
        return subprocess.run([*aws_cli(), *sys.argv[2:]]).returncode
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=(
        "deploy", "destroy", "login", "storage", "secrets", "runs", "logs",
        "pause", "unpause", "start"))
    parser.add_argument("app")
    parser.add_argument("rest", nargs="*")
    parser.add_argument("--yes", action="store_true")
    args = parser.parse_intermixed_args()
    app = load_app(args.app)
    if args.command == "login":
        return relogin(app)
    if args.command == "storage":
        session = ensure_session(app)
        account, _region = aws_settings(app, session)
        return storage(app, session, account, args.rest, args.yes)
    from pdt import deploy_aws_batch as batch
    try:
        if args.command == "secrets":
            return batch.secrets(app, args.rest[0], args.yes, *args.rest[1:])
        if args.command == "deploy":
            return batch.deploy(app, args.yes)
        if args.command == "runs":
            return batch.runs(app, ensure_session(app), args.rest)
        if args.command == "logs":
            return batch.logs(app, ensure_session(app), args.rest)
        if args.command in ("pause", "unpause"):
            return batch.pause(app, ensure_session(app), args.command == "pause")
        if args.command == "start":
            return batch.start(app, ensure_session(app))
        return batch.destroy(app, args.yes)
    except ClientError as exc:
        if error_code(exc) in {"AccessDenied", "AccessDeniedException",
                               "UnauthorizedOperation"}:
            session = boto3.Session(profile_name=app["platform"].get("profile") or None)
            account, region = aws_settings(app, session)
            identity = "the current AWS login"
            try:
                identity = session.client("sts").get_caller_identity()["Arn"]
            except Exception:  # noqa: BLE001,S110 - retain the original permission error
                pass
            print_permission_help(identity, str(exc), batch.DEPLOYER_ACTIONS, account, region)
            return 1
        fail(f"AWS returned {error_code(exc) or 'an error'}: {exc}")


if __name__ == "__main__":
    sys.exit(main())
