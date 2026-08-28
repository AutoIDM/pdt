"""Deploy an app as a scheduled AWS Lambda function.

Selected with platform.runtime: lambda (the default). Entered through
deploy_aws.py, which owns the uv script header, login, and permission handling.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import shutil
import tempfile
import time
import zipfile
from pathlib import Path

from botocore.exceptions import ClientError

from pdt import config
from pdt.deploy import confirm
from pdt.deploy_aws import (
    COMMON_ACTIONS, MANAGED_TAGS, RECENT_RUNS, SCHEDULE_GROUP,
    aws_schedule_expression, aws_settings, clients_for, cost_estimate_lines,
    delete_log_group, list_price, run_basis,
    delete_role, delete_secret, ensure_log_group, ensure_role, ensure_schedule,
    ensure_secret, ensure_session, not_found, preflight,
    resource_exists, other_schedules, delete_schedule_group,
    with_role_propagation_retry,
)
from pdt.deploy_common import fail, gather_secrets, run_build, stage_build_context

LAMBDA_MEMORY_MB = 512
LAMBDA_TIMEOUT_SECONDS = 900
LAMBDA_RUNTIME = "python3.12"
LAMBDA_ARCHITECTURE = "arm64"
LAMBDA_PLATFORM = "aarch64-manylinux2014"
MAX_ZIP_BYTES = 50 * 1024 * 1024
MAX_UNZIPPED_BYTES = 250 * 1024 * 1024
LAMBDA_ACTIONS = [
    "lambda:CreateFunction",
    "lambda:DeleteFunction",
    "lambda:GetFunction",
    "lambda:ListTags",
    "lambda:TagResource",
    "lambda:UpdateFunctionCode",
    "lambda:UpdateFunctionConfiguration",
]
DEPLOYER_ACTIONS = sorted(COMMON_ACTIONS + LAMBDA_ACTIONS)
LAMBDA_HANDLER = """\
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

import boto3

_APP = None
os.environ.setdefault("PDT_PROJECT", str(Path(__file__).parent))


def _app():
    global _APP
    if _APP is None:
        path = Path(__file__).parent / {app_name!r} / "run.py"
        spec = importlib.util.spec_from_file_location("pdt_app", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _APP = module
    return _APP


def lambda_handler(_event, _context):
    secret = boto3.client("secretsmanager").get_secret_value(
        SecretId=os.environ["PDT_SECRET_ARN"]
    )["SecretString"]
    os.environ["PDT_ENV_JSON"] = secret
    for name, value in json.loads(secret).items():
        os.environ[name] = str(value)
    status = _app().main()
    if status:
        raise RuntimeError(f"app exited with status {{status}}")
    return {{"status": "ok"}}
"""



def resource_names(app_name: str) -> dict[str, str]:
    base = f"pdt-{app_name}"
    return {
        "function": base,
        "schedule": base,
        "secret": f"{base}-env",
        "log_group": f"/aws/lambda/{base}",
        "function_role": f"{base}-function",
        "scheduler_role": f"{base}-scheduler",
    }


def ensure_roles(iam, names: dict[str, str], account: str, region: str,
                 secret_arn: str) -> tuple[str, str]:
    log_arn = f"arn:aws:logs:{region}:{account}:log-group:{names['log_group']}:*"
    function = ensure_role(
        iam, names["function_role"], "lambda.amazonaws.com", "pdt-function", [
            {"Effect": "Allow", "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
             "Resource": log_arn},
            {"Effect": "Allow", "Action": ["secretsmanager:GetSecretValue"],
             "Resource": secret_arn},
        ])
    function_arn = f"arn:aws:lambda:{region}:{account}:function:{names['function']}"
    scheduler = ensure_role(
        iam, names["scheduler_role"], "scheduler.amazonaws.com", "pdt-scheduler", [
            {"Effect": "Allow", "Action": ["lambda:InvokeFunction"],
             "Resource": function_arn},
        ])
    return function, scheduler


def build_package(app: dict) -> tuple[bytes, str, int]:
    stage = stage_build_context(app)
    (stage / "lambda_function.py").write_text(
        LAMBDA_HANDLER.format(app_name=app["name"]))
    work = Path(tempfile.mkdtemp(prefix="pdt-requirements-"))
    archive = work / "function.zip"
    requirements = work / "requirements.txt"
    try:
        run_build([
            "uv", "export", "--script", str(app["dir"] / "run.py"),
            "--format", "requirements-txt", "--no-hashes",
            "--output-file", str(requirements),
        ])
        run_build([
            "uv", "pip", "install", "--requirements", str(requirements),
            "--target", str(stage), "--python", "3.12",
            "--python-platform", LAMBDA_PLATFORM, "--only-binary", ":all:",
            "--no-compile-bytecode", "--no-installer-metadata",
        ])
        uncompressed = sum(path.stat().st_size for path in stage.rglob("*")
                           if path.is_file())
        if uncompressed > MAX_UNZIPPED_BYTES:
            fail("Lambda package exceeds the 250 MB uncompressed limit")
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
            for path in sorted(item for item in stage.rglob("*") if item.is_file()):
                info = zipfile.ZipInfo(path.relative_to(stage).as_posix())
                info.date_time = (2020, 1, 1, 0, 0, 0)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o644 << 16
                zf.writestr(info, path.read_bytes())
        package = archive.read_bytes()
        if len(package) > MAX_ZIP_BYTES:
            fail("Lambda zip exceeds the 50 MB direct-upload limit")
        digest = base64.b64encode(hashlib.sha256(package).digest()).decode("ascii")
        return package, digest, uncompressed
    finally:
        shutil.rmtree(stage, ignore_errors=True)
        shutil.rmtree(work, ignore_errors=True)


def ensure_function(lambda_client, names: dict[str, str], role_arn: str,
                    secret_arn: str, package: bytes, digest: str) -> str:
    function_name = names["function"]
    try:
        current = lambda_client.get_function(FunctionName=function_name)
        arn = current["Configuration"]["FunctionArn"]
        tags = lambda_client.list_tags(Resource=arn).get("Tags", {})
        if tags.get("managed-by") != "pdt":
            fail(f"Lambda function {function_name} exists but is not managed by PDT")
        architectures = current["Configuration"].get("Architectures", ["x86_64"])
        if (current["Configuration"].get("CodeSha256") != digest
                or architectures != [LAMBDA_ARCHITECTURE]):
            lambda_client.update_function_code(
                FunctionName=function_name, ZipFile=package,
                Architectures=[LAMBDA_ARCHITECTURE])
            lambda_client.get_waiter("function_updated").wait(
                FunctionName=function_name)
        lambda_client.update_function_configuration(
            FunctionName=function_name,
            Role=role_arn,
            Runtime=LAMBDA_RUNTIME,
            Handler="lambda_function.lambda_handler",
            Timeout=LAMBDA_TIMEOUT_SECONDS,
            MemorySize=LAMBDA_MEMORY_MB,
            Environment={"Variables": {"PDT_SECRET_ARN": secret_arn}},
        )
        lambda_client.get_waiter("function_updated").wait(
            FunctionName=function_name)
        lambda_client.tag_resource(Resource=arn, Tags=MANAGED_TAGS)
        return arn
    except Exception as exc:
        if not not_found(exc):
            raise
    created = with_role_propagation_retry(lambda: lambda_client.create_function(
        FunctionName=function_name,
        Runtime=LAMBDA_RUNTIME,
        Role=role_arn,
        Handler="lambda_function.lambda_handler",
        Code={"ZipFile": package},
        Description="Managed by PDT",
        Timeout=LAMBDA_TIMEOUT_SECONDS,
        MemorySize=LAMBDA_MEMORY_MB,
        Publish=False,
        Environment={"Variables": {"PDT_SECRET_ARN": secret_arn}},
        Architectures=[LAMBDA_ARCHITECTURE],
        Tags=MANAGED_TAGS,
    ))
    return created["FunctionArn"]


def recent_report_seconds(logs, log_group: str) -> float | None:
    # Lambda reuses one log stream for many runs; each run ends with a REPORT line.
    try:
        events = logs.filter_log_events(
            logGroupName=log_group, filterPattern="REPORT",
            startTime=int((time.time() - 30 * 86400) * 1000)).get("events", [])
    except ClientError as exc:
        if not_found(exc):
            return None
        raise
    durations = []
    for event in events[-RECENT_RUNS:]:
        match = re.search(r"Duration: ([\d.]+) ms", event["message"])
        if match:
            durations.append(float(match.group(1)) / 1000)
    if not durations:
        return None
    return sum(durations) / len(durations)


def cost_lines(logs, names: dict[str, str], region: str, cron: str,
               function_exists: bool) -> list[str]:
    print("Fetching list prices from the AWS price list...")
    try:
        runs = config.runs_per_month(cron)
        seconds, basis = run_basis(
            recent_report_seconds(logs, names["log_group"]) if function_exists else None)
        gib = LAMBDA_MEMORY_MB / 1024
        compute = runs * seconds * gib * list_price("AWSLambda", region, "Lambda-GB-Second-ARM")
        secret = list_price("AWSSecretsManager", region, "AWSSecretsManager-Secrets")
        items = [
            (f"Lambda ({LAMBDA_ARCHITECTURE}): ~{runs:.0f} runs x {basis} x {gib:g} GiB", compute),
            ("Secrets Manager: 1 secret", secret),
        ]
    except Exception as exc:
        fail(f"could not calculate the required monthly cost estimate: {exc}")
    return cost_estimate_lines(
        region, items, "excludes EventBridge Scheduler free tier and CloudWatch Logs usage")


def deploy(app: dict, assume_yes: bool, profile: str | None = None) -> int:
    session = ensure_session(app, profile)
    expected_account, region = aws_settings(app, session)
    clients = clients_for(session)
    account, _identity = preflight(
        clients["sts"], clients["iam"], expected_account, DEPLOYER_ACTIONS)
    names = resource_names(app["name"])
    cron = config.cron_expression(app["schedule"])
    expression = aws_schedule_expression(cron)
    payload = json.dumps(gather_secrets(app), sort_keys=True)

    print(f"Checking current state in account {account} ({region})...")
    function_exists = resource_exists(
        clients["lambda"], "get_function", FunctionName=names["function"])
    schedule_exists = resource_exists(
        clients["scheduler"], "get_schedule",
        Name=names["schedule"], GroupName=SCHEDULE_GROUP)
    secret_exists = resource_exists(
        clients["secretsmanager"], "describe_secret", SecretId=names["secret"])
    actions = [
        "build a Docker-free Lambda zip on this computer",
        ("update" if secret_exists else "create")
        + f" Secrets Manager secret {names['secret']}",
        "reconcile the Lambda function and scheduler IAM roles",
        ("update" if function_exists else "create")
        + f" Lambda function {names['function']} "
        + f"({LAMBDA_ARCHITECTURE}, {LAMBDA_MEMORY_MB} MiB, 15-minute limit)",
        ("update" if schedule_exists else "create")
        + f" EventBridge schedule {names['schedule']}: {expression} ({app['timezone']})",
    ]
    if not confirm(actions, assume_yes, cost_lines(
            clients["logs"], names, region, cron, function_exists)):
        print("Aborted; nothing was changed.")
        return 1

    print("==> building Lambda zip")
    package, digest, uncompressed = build_package(app)
    print(f"    {len(package) / 1024 / 1024:.1f} MB compressed; "
          f"{uncompressed / 1024 / 1024:.1f} MB uncompressed")
    print(f"==> reconciling AWS resources in {account} ({region})")
    ensure_log_group(clients["logs"], names["log_group"])
    secret_arn = ensure_secret(
        clients["secretsmanager"], names["secret"], payload)
    function_role, scheduler_role = ensure_roles(
        clients["iam"], names, account, region, secret_arn)
    function_arn = ensure_function(
        clients["lambda"], names, function_role, secret_arn, package, digest)
    ensure_schedule(
        clients["scheduler"], names["schedule"], expression, app["timezone"],
        scheduler_role, {"Arn": function_arn})
    print(f"Deployed {app['name']}.")
    print(f"Run it once: aws lambda invoke --function-name {names['function']} "
          f"--region {region} response.json")
    return 0


def destroy(app: dict, assume_yes: bool, profile: str | None = None) -> int:
    session = ensure_session(app, profile)
    expected_account, region = aws_settings(app, session)
    clients = clients_for(session)
    account, _identity = preflight(
        clients["sts"], clients["iam"], expected_account, DEPLOYER_ACTIONS)
    names = resource_names(app["name"])
    schedule_exists = resource_exists(
        clients["scheduler"], "get_schedule",
        Name=names["schedule"], GroupName=SCHEDULE_GROUP)
    function_exists = resource_exists(
        clients["lambda"], "get_function", FunctionName=names["function"])
    secret_exists = resource_exists(
        clients["secretsmanager"], "describe_secret", SecretId=names["secret"])
    actions = []
    if schedule_exists:
        actions.append(f"delete EventBridge schedule {names['schedule']}")
    if function_exists:
        actions.append(f"delete Lambda function {names['function']}")
    if secret_exists:
        actions.append(f"delete secret {names['secret']}")
    actions += [
        f"delete tagged log group {names['log_group']}",
        "delete tagged per-app IAM roles",
    ]
    others = other_schedules(clients["scheduler"], names["schedule"])
    if others == []:
        actions.append(f"delete schedule group {SCHEDULE_GROUP} (no other apps use it)")
    if not confirm(actions, assume_yes):
        print("Aborted; nothing was changed.")
        return 1

    scheduler = clients["scheduler"]
    if schedule_exists:
        scheduler.delete_schedule(
            Name=names["schedule"], GroupName=SCHEDULE_GROUP)
    lambda_client = clients["lambda"]
    if function_exists:
        current = lambda_client.get_function(FunctionName=names["function"])
        arn = current["Configuration"]["FunctionArn"]
        tags = lambda_client.list_tags(Resource=arn).get("Tags", {})
        if tags.get("managed-by") == "pdt":
            lambda_client.delete_function(FunctionName=names["function"])
    delete_secret(clients["secretsmanager"], names["secret"])
    delete_log_group(clients["logs"], names["log_group"])
    iam = clients["iam"]
    delete_role(iam, names["scheduler_role"])
    delete_role(iam, names["function_role"])
    if others == []:
        delete_schedule_group(clients["scheduler"])
    print(f"Removed {app['name']} from account {account} ({region}).")
    return 0
