#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "boto3",
#     "pyyaml",
#     "python-dotenv",
# ]
# ///
"""Deploy an app to AWS.

Shared login, IAM, secret, log, and schedule code lives here. The
runtime-specific parts are in deploy_aws_lambda.py and deploy_aws_fargate.py.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pdt import config
from pdt.deploy_common import fail

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
]
def error_code(exc: Exception) -> str:
    return getattr(exc, "response", {}).get("Error", {}).get("Code", "")


def not_found(exc: Exception) -> bool:
    return error_code(exc) in {
        "NoSuchEntity", "ResourceNotFoundException", "ResourceNotFound",
        "ClusterNotFoundException", "RepositoryNotFoundException",
    }


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


def with_role_propagation_retry(operation, sleep=time.sleep):
    for delay in (*ROLE_PROPAGATION_DELAYS, None):
        try:
            return operation()
        except Exception as exc:
            if delay is None or not role_propagation_error(exc):
                raise
            print(f"    IAM role is not visible yet; retrying in {delay}s...")
            sleep(delay)
    raise AssertionError("unreachable")


def adopt_account(app: dict, session) -> str:
    # AWS gives one account per credential set, so there is nothing to pick.
    identity = session.client("sts").get_caller_identity()
    account = identity["Account"]
    print(f"These credentials belong to AWS account {account}.")
    print(f"  {identity['Arn']}")
    saved = config.save_platform_key(app, "account", account)
    print(f"Saved account {account} to {saved.relative_to(config.find_project())}.")
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


def deployer_policy(actions: list[str]) -> dict:
    return {
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Action": actions,
            "Resource": "*",
        }],
    }


def print_permission_help(identity: str, detail: str, actions: list[str]) -> None:
    print("AWS blocked this deployment because the current login lacks a permission.")
    if detail:
        print(f"AWS said: {detail}")
    print(f"Current AWS login: {identity}")
    print("Send the policy below to the person who manages your AWS account.")
    print("Ask them to add it to this login, then run the same command again.")
    print(json.dumps(deployer_policy(actions), indent=2))


def principal_arn(identity_arn: str, account: str) -> str:
    marker = f"arn:aws:sts::{account}:assumed-role/"
    if identity_arn.startswith(marker):
        role_name = identity_arn[len(marker):].split("/", 1)[0]
        return f"arn:aws:iam::{account}:role/{role_name}"
    return identity_arn


def ask(prompt: str) -> str:
    try:
        return input(prompt).strip()
    except EOFError:
        return ""


def choose_profile(session) -> str:
    profiles = session.available_profiles
    if not profiles:
        print("No AWS credentials or profiles were found on this computer.")
        print("Create a profile first:  aws configure sso   (or: aws configure)")
        print("Then select it:          export AWS_PROFILE=<profile-name>")
        fail("run the same command again after you set AWS_PROFILE")
    if len(profiles) == 1:
        print(f"Using the only AWS profile on this computer: {profiles[0]}")
        return profiles[0]
    print("No AWS profile is selected. Profiles on this computer:")
    for number, profile in enumerate(profiles, start=1):
        print(f"  {number}) {profile}")
    answer = ask(f"Which profile do you want to use? [1-{len(profiles)}] ")
    if answer.isdigit() and 1 <= int(answer) <= len(profiles):
        profile = profiles[int(answer) - 1]
    elif answer in profiles:
        profile = answer
    else:
        fail("no AWS profile selected; run again with --profile <name> "
             "or set AWS_PROFILE=<name>")
    print(f"To skip this question next time:  --profile {profile}  "
          f"or  export AWS_PROFILE={profile}")
    return profile


def sso_login(profile: str | None) -> bool:
    aws_cli = shutil.which("aws")
    if not aws_cli:
        return False
    command = [aws_cli, "sso", "login"]
    if profile:
        command += ["--profile", profile]
    print("Your AWS login has expired or is missing.")
    shown = " ".join(["aws", *command[1:]])
    answer = ask(f"Log in now with `{shown}` (opens a browser)? [y/N] ")
    if answer.lower() not in ("y", "yes"):
        return False
    return subprocess.run(command, check=False).returncode == 0


def relogin(profile: str | None) -> int:
    session = boto3.Session()
    name = profile or os.environ.get("AWS_PROFILE") or ""
    if name not in session.available_profiles:
        name = choose_profile(session)
    aws_cli = shutil.which("aws")
    if not aws_cli:
        print("The AWS CLI is not installed, so pdt cannot refresh an SSO login here.")
        print("Install it from https://docs.aws.amazon.com/cli/latest/"
              "userguide/getting-started-install.html")
        fail(f"then run: aws sso login --profile {name}")
    print(f"Logging in to AWS profile {name}...")
    if subprocess.run([aws_cli, "sso", "login", "--profile", name]).returncode != 0:
        print(f"If {name} uses access keys instead of SSO there is no login to "
              f"refresh; run `aws configure --profile {name}` to replace the keys.")
        fail("aws sso login failed")
    identity = boto3.Session(profile_name=name).client("sts").get_caller_identity()
    print(f"Signed in as {identity['Arn']}")
    print(f"Account {identity['Account']}")
    return 0


def login_error(exc: Exception) -> bool:
    name = type(exc).__name__
    return ("SSO" in name or "Token" in name
            or error_code(exc) in {"ExpiredToken", "ExpiredTokenException",
                                   "InvalidClientTokenId", "UnrecognizedClientException"})


def ensure_session(app: dict, profile: str | None = None):
    region = app["platform"].get("region") or None
    if profile and profile not in boto3.Session().available_profiles:
        fail(f"AWS profile {profile!r} not found; profiles on this computer: "
             + ", ".join(boto3.Session().available_profiles))
    session = boto3.Session(region_name=region, profile_name=profile)
    if session.get_credentials() is None:
        session = boto3.Session(region_name=region, profile_name=choose_profile(session))
    try:
        session.client("sts").get_caller_identity()
    except Exception as exc:  # noqa: BLE001 - credential providers raise several types
        if not login_error(exc) or not sso_login(session.profile_name):
            profile = session.profile_name or "<profile-name>"
            fail(f"AWS credentials are unavailable or invalid: {exc}\n"
                 f"Log in first (for example: aws sso login --profile {profile}), "
                 f"then run the same command again.")
        session = boto3.Session(region_name=region, profile_name=session.profile_name)
    return session


def preflight(sts, iam, expected_account: str, actions: list[str]) -> tuple[str, str]:
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
    try:
        result = iam.simulate_principal_policy(
            PolicySourceArn=source_arn,
            ActionNames=actions,
        )
    except ClientError as exc:
        if error_code(exc) in {"AccessDenied", "AccessDeniedException"}:
            print_permission_help(identity_arn, str(exc), actions)
            raise SystemExit(1) from exc
        raise
    denied = sorted(item["EvalActionName"] for item in result["EvaluationResults"]
                    if item["EvalDecision"] != "allowed")
    if denied:
        print_permission_help(identity_arn, "These required actions are not allowed: "
                              + ", ".join(denied), actions)
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


def ensure_log_group(logs, name: str) -> None:
    groups = logs.describe_log_groups(logGroupNamePrefix=name).get("logGroups", [])
    exists = any(group["logGroupName"] == name for group in groups)
    if exists:
        group = next(group for group in groups if group["logGroupName"] == name)
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
        Description="PDT_ENV_JSON for a PDT Lambda function",
    )["ARN"]


def list_price(offer: str, region: str, usagetype_suffix: str) -> float:
    url = PRICE_LIST_URL.format(offer=offer, region=region)
    with urllib.request.urlopen(url, timeout=60) as resp:
        data = json.load(resp)
    for sku, product in data["products"].items():
        if not product["attributes"].get("usagetype", "").endswith(usagetype_suffix):
            continue
        for term in data["terms"]["OnDemand"].get(sku, {}).values():
            for dimension in term["priceDimensions"].values():
                if dimension.get("beginRange", "0") == "0":
                    return float(dimension["pricePerUnit"]["USD"])
    raise LookupError(f"no {usagetype_suffix!r} price for {offer} in region {region}")


def recent_stream_seconds(logs, log_group: str) -> float | None:
    # One log stream per run (Fargate task); its first and last event bound the run.
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


def cost_estimate_lines(region: str, items: list[tuple[str, float]],
                        excludes: str) -> list[str]:
    total = sum(cost for _, cost in items)
    width = max(len(label) for label, _ in items)
    lines = [f"Estimated monthly cost ({region} list prices, before free tiers):"]
    for label, cost in items:
        lines.append(f"  {label:<{width}}  ${cost:>7.2f}")
    lines.append(f"  {'total':<{width}}  ${total:>7.2f}")
    lines.append(f"  ({excludes})")
    return lines


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
                    role_arn: str, target: dict) -> None:
    # Scheduler tags live on groups, not schedules: membership in the
    # tagged pdt group is the ownership marker.
    ensure_schedule_group(scheduler)
    request = {
        "Name": name,
        "GroupName": SCHEDULE_GROUP,
        "ScheduleExpression": expression,
        "ScheduleExpressionTimezone": timezone,
        "FlexibleTimeWindow": {"Mode": "OFF"},
        "State": "ENABLED",
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
            ("sts", "lambda", "logs", "secretsmanager", "iam", "scheduler")}


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
    groups = logs.describe_log_groups(logGroupNamePrefix=name).get("logGroups", [])
    for group in groups:
        if group["logGroupName"] != name:
            continue
        arn = group.get("logGroupArn") or group["arn"].removesuffix(":*")
        tags = logs.list_tags_for_resource(resourceArn=arn).get("tags", {})
        if tags.get("managed-by") == "pdt":
            logs.delete_log_group(logGroupName=name)


def other_schedules(scheduler, name: str) -> list[str] | None:
    """Names of other schedules in the pdt group; None when the group is absent."""
    try:
        schedules = scheduler.list_schedules(GroupName=SCHEDULE_GROUP).get("Schedules", [])
    except Exception as exc:
        if not not_found(exc):
            raise
        return None
    return [item["Name"] for item in schedules if item["Name"] != name]


def delete_schedule_group(scheduler) -> None:
    try:
        scheduler.delete_schedule_group(Name=SCHEDULE_GROUP)
    except Exception as exc:
        if not not_found(exc):
            raise


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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("deploy", "destroy", "login"))
    parser.add_argument("app")
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--profile", help="AWS profile name")
    args = parser.parse_args()
    if args.command == "login":
        return relogin(args.profile)
    app = load_app(args.app)
    if app["platform"].get("runtime", "lambda") == "fargate":
        from pdt import deploy_aws_fargate as runtime
    else:
        from pdt import deploy_aws_lambda as runtime
    try:
        if args.command == "deploy":
            return runtime.deploy(app, args.yes, args.profile)
        return runtime.destroy(app, args.yes, args.profile)
    except ClientError as exc:
        if error_code(exc) in {"AccessDenied", "AccessDeniedException",
                               "UnauthorizedOperation"}:
            identity = "the current AWS login"
            try:
                identity = boto3.client("sts").get_caller_identity()["Arn"]
            except Exception:  # noqa: BLE001,S110 - retain the original permission error
                pass
            print_permission_help(identity, str(exc), runtime.DEPLOYER_ACTIONS)
            return 1
        fail(f"AWS returned {error_code(exc) or 'an error'}: {exc}")


if __name__ == "__main__":
    sys.exit(main())
