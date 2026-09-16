"""List what a real cloud account holds, one function per provider.

Every cloud listing runs through pdt's own CLI passthroughs (`pdt aws`,
`pdt az`, `pdt gcloud`), so verifying a provider needs nothing installed
that deploying it does not already install. The Windows provider deploys
to the runner itself, so it reads Task Scheduler through PowerShell.

A listing covers only the names pdt creates, because the same account may
hold resources that have nothing to do with pdt.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import subprocess
from dataclasses import dataclass, field


MANAGED = {"managed-by": "pdt"}
UNTAGGED = "untagged"
SHARED = "shared"
# The Container Apps environment every pdt project in a subscription shares.
AZURE_SHARED_GROUP = "pdt-shared"
GOOGLE_ASSET_TYPES = (
    "run.googleapis.com/Job",
    "secretmanager.googleapis.com/Secret",
    "artifactregistry.googleapis.com/Repository",
    "storage.googleapis.com/Bucket",
)
GREETING = "Hello from pdt."
WINDOWS_TASKS = (
    "$tasks = @(Get-ScheduledTask -TaskPath '\\' | "
    "Where-Object { $_.TaskName -like 'pdt-*' } | "
    "Select-Object TaskName, TaskPath, Description); "
    "ConvertTo-Json -InputObject $tasks -Compress"
)
WINDOWS_TASK_INFO = (
    "Get-ScheduledTaskInfo -TaskName '{task}' | Select-Object "
    "@{{n='LastRunTime';e={{$_.LastRunTime.ToUniversalTime().ToString('o')}}}}, "
    "LastTaskResult | ConvertTo-Json -Compress"
)


def resource_prefix() -> str:
    namespace = os.environ.get("PDT_RESOURCE_NAMESPACE", "").strip()
    return f"pdt-{namespace}" if namespace else "pdt"


def has_prefix(value: str) -> bool:
    prefix = resource_prefix()
    return value == prefix or value.startswith(f"{prefix}-")


def resource_name(app: str) -> str:
    return f"{resource_prefix()}-{app}"


def azure_job_name(app: str) -> str:
    name = resource_name(app)
    if len(name) <= 32:
        return name
    suffix = hashlib.sha256(name.encode()).hexdigest()[:7]
    return f"{name[:24].rstrip('-')}-{suffix}"


def store_name(seed: str) -> str:
    namespace = os.environ.get("PDT_RESOURCE_NAMESPACE", "").strip()
    value = f"{namespace}/{seed}" if namespace else seed
    suffix = hashlib.sha256(value.encode()).hexdigest()[:10]
    prefix = "pdt-data" if not namespace else f"{resource_prefix()}-data"
    return f"{prefix}-{suffix}"


def azure_store_names(subscription: str) -> tuple[str, str, str]:
    namespace = os.environ.get("PDT_RESOURCE_NAMESPACE", "").strip()
    value = f"{namespace}/{subscription}" if namespace else subscription
    suffix = hashlib.sha256(value.encode()).hexdigest()[:10]
    prefix = resource_prefix()
    group = "pdt-data" if prefix == "pdt" else f"{prefix}-data"
    account = f"pdtdata{suffix}"
    return group, account, f"{group}-{suffix}"


class InventoryError(Exception):
    pass


@dataclass
class Resource:
    kind: str
    id: str
    tags: dict[str, str] = field(default_factory=dict)
    name: str = ""


Inventory = list[Resource]
Owner = str


def classify(resource: Resource, apps: list[str]) -> Owner:
    if resource.tags.get("managed-by") != "pdt":
        return UNTAGGED
    tagged = resource.tags.get("pdt-app")
    if tagged in apps:
        return tagged
    named = [app for app in apps
             if f"{resource_prefix()}-{app}" in resource.id
             or f"{resource_prefix()}-{app}" in resource.name]
    if len(named) == 1:
        return named[0]
    return SHARED


def run_json(command: list[str]):
    proc = subprocess.run(command, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        raise InventoryError(f"{' '.join(command[:4])} failed: {detail}")
    return json.loads(proc.stdout or "null")


def aws(region: str, *args: str):
    return run_json(["pdt", "aws", *args, "--region", region, "--output", "json"])


def az(*args: str):
    return run_json(["pdt", "az", *args, "--output", "json"])


def gcloud(*args: str):
    return run_json(["pdt", "gcloud", *args, "--format", "json"])


def aws_functions(region: str) -> Inventory:
    found = []
    for item in (aws(region, "lambda", "list-functions") or {}).get("Functions") or []:
        if not has_prefix(item["FunctionName"]):
            continue
        arn = item["FunctionArn"]
        tags = (aws(region, "lambda", "list-tags", "--resource", arn) or {}).get("Tags") or {}
        found.append(Resource("lambda function", arn, tags, item["FunctionName"]))
    return found


def aws_caller_role(region: str) -> str:
    arn = (aws(region, "sts", "get-caller-identity") or {}).get("Arn") or ""
    marker = ":assumed-role/"
    if marker not in arn:
        return ""
    return arn.split(marker, 1)[1].split("/", 1)[0]


def aws_roles(region: str) -> Inventory:
    # The role this run signs in with may share the pdt- prefix, and it is
    # not something deploy made.
    own = aws_caller_role(region)
    found = []
    for item in (aws(region, "iam", "list-roles") or {}).get("Roles") or []:
        name = item["RoleName"]
        if not has_prefix(name) or name == own:
            continue
        listed = aws(region, "iam", "list-role-tags", "--role-name", name) or {}
        tags = {tag["Key"]: tag["Value"] for tag in listed.get("Tags") or []}
        found.append(Resource("iam role", item["Arn"], tags, name))
    return found


def aws_log_groups(region: str) -> Inventory:
    found = []
    for prefix in (f"/aws/lambda/{resource_prefix()}-",
                   f"/ecs/{resource_prefix()}-", f"/{resource_prefix()}/"):
        listed = aws(region, "logs", "describe-log-groups",
                     "--log-group-name-prefix", prefix) or {}
        for item in listed.get("logGroups") or []:
            arn = (item.get("logGroupArn") or item["arn"]).removesuffix(":*")
            tags = (aws(region, "logs", "list-tags-for-resource",
                        "--resource-arn", arn) or {}).get("tags") or {}
            found.append(Resource("log group", arn, tags, item["logGroupName"]))
    return found


def aws_secrets(region: str) -> Inventory:
    found = []
    for item in (aws(region, "secretsmanager", "list-secrets") or {}).get("SecretList") or []:
        if not has_prefix(item["Name"]):
            continue
        tags = {tag["Key"]: tag["Value"] for tag in item.get("Tags") or []}
        found.append(Resource("secret", item["ARN"], tags, item["Name"]))
    return found


def aws_schedules(region: str) -> Inventory:
    found = []
    listed = aws(region, "scheduler", "list-schedule-groups") or {}
    for group in listed.get("ScheduleGroups") or []:
        if group["Name"] != resource_prefix():
            continue
        try:
            tagged = aws(region, "scheduler", "list-tags-for-resource",
                         "--resource-arn", group["Arn"]) or {}
            schedules = aws(region, "scheduler", "list-schedules",
                            "--group-name", group["Name"]) or {}
        except InventoryError as error:
            if "(ResourceNotFoundException)" not in str(error):
                raise
            found.append(Resource("schedule group", group["Arn"], {}, group["Name"]))
            continue
        tags = {tag["Key"]: tag["Value"] for tag in tagged.get("Tags") or []}
        found.append(Resource("schedule group", group["Arn"], tags, group["Name"]))
        for item in schedules.get("Schedules") or []:
            # pdt tags the group, not the schedule; membership is the marker.
            found.append(Resource("schedule", item["Arn"], dict(MANAGED), item["Name"]))
    return found


def aws_ecs_tags(region: str, arn: str) -> dict[str, str]:
    listed = aws(region, "ecs", "list-tags-for-resource", "--resource-arn", arn) or {}
    return {tag["key"]: tag["value"] for tag in listed.get("tags") or []}


def aws_clusters(region: str) -> Inventory:
    arns = (aws(region, "ecs", "list-clusters") or {}).get("clusterArns") or []
    if not arns:
        return []
    described = aws(region, "ecs", "describe-clusters", "--clusters", *arns) or {}
    found = []
    for item in described.get("clusters") or []:
        arn, name = item["clusterArn"], item["clusterName"]
        if name != resource_prefix() or item.get("status") != "ACTIVE":
            continue
        found.append(Resource("ecs cluster", arn, aws_ecs_tags(region, arn), name))
    return found


def aws_task_definitions(region: str) -> Inventory:
    listed = aws(region, "ecs", "list-task-definitions",
                 "--family-prefix", f"{resource_prefix()}-", "--status", "ACTIVE") or {}
    return [Resource("ecs task definition", arn, aws_ecs_tags(region, arn),
                     arn.rsplit("/", 1)[-1])
            for arn in listed.get("taskDefinitionArns") or []]


def aws_repositories(region: str) -> Inventory:
    found = []
    for item in (aws(region, "ecr", "describe-repositories") or {}).get("repositories") or []:
        name = item["repositoryName"]
        if not has_prefix(name):
            continue
        arn = item["repositoryArn"]
        listed = aws(region, "ecr", "list-tags-for-resource", "--resource-arn", arn) or {}
        tags = {tag["Key"]: tag["Value"] for tag in listed.get("tags") or []}
        found.append(Resource("ecr repository", arn, tags, name))
    return found


def aws_stores(region: str) -> Inventory:
    identity = aws(region, "sts", "get-caller-identity") or {}
    account = str(identity.get("Account") or "")
    if not account:
        return []
    name = store_name(account)
    try:
        aws(region, "s3api", "head-bucket", "--bucket", name)
    except InventoryError as error:
        if "(404)" in str(error) or "Not Found" in str(error):
            return []
        raise
    try:
        tags = {tag["Key"]: tag["Value"] for tag in (
            aws(region, "s3api", "get-bucket-tagging", "--bucket", name)
            or {}).get("TagSet") or []}
    except InventoryError:
        tags = {}
    return [Resource("s3 bucket", f"arn:aws:s3:::{name}", tags, name)]


def aws_tagged(region: str) -> Inventory:
    listed = aws(region, "resourcegroupstaggingapi", "get-resources",
                 "--tag-filters", "Key=managed-by,Values=pdt") or {}
    found = []
    for item in listed.get("ResourceTagMappingList") or []:
        arn = item["ResourceARN"]
        name = arn.rsplit("/", 1)[-1].rsplit(":", 1)[-1]
        if not has_prefix(name):
            continue
        # ECS keeps a deleted cluster or task definition visible as INACTIVE,
        # and this API still returns it. The ECS listings above decide those.
        if arn.split(":")[2] == "ecs":
            continue
        tags = {tag["Key"]: tag["Value"] for tag in item.get("Tags") or []}
        found.append(Resource(arn.split(":")[2], arn, tags, name))
    return found


AWS_SOURCES = (
    aws_functions, aws_roles, aws_log_groups, aws_secrets, aws_schedules,
    aws_clusters, aws_task_definitions, aws_repositories, aws_stores, aws_tagged,
)


def aws_inventory(settings: dict[str, str]) -> Inventory:
    # The tagging API omits an untagged leftover, so the per-kind listings
    # above it decide what exists and it only adds what they do not cover.
    found: dict[str, Resource] = {}
    for source in AWS_SOURCES:
        for resource in source(settings["region"]):
            found.setdefault(resource.id, resource)
    return sorted(found.values(), key=lambda resource: resource.id)


def azure_deleted_vaults(settings: dict[str, str]) -> Inventory:
    # A soft-deleted vault still owns its global name and blocks the next
    # deploy, so it counts as a leftover.
    seed = (f"{settings['subscription']}/{settings['resource_group']}"
            if settings.get("subscription") else settings["resource_group"])
    expected = (os.environ.get("PDT_AZURE_KEY_VAULT", "").strip()
                or f"pdt-{hashlib.sha256(seed.encode()).hexdigest()[:10]}")
    found = []
    for item in az("keyvault", "list-deleted", "--resource-type", "vault") or []:
        if item["name"] != expected:
            continue
        tags = (item.get("properties") or {}).get("tags") or {}
        found.append(Resource("soft-deleted key vault", item["id"], tags, item["name"]))
    return found


def azure_inventory(settings: dict[str, str]) -> Inventory:
    found = azure_deleted_vaults(settings)
    # A named environment is the user's own, so its group is not pdt's to empty.
    groups = [settings["resource_group"]]
    subscription = settings.get("subscription", "")
    if "subscription" in settings and not subscription:
        account = az("account", "show") or {}
        subscription = str(account.get("id") or "") if isinstance(account, dict) else ""
    store_group, _account, _container = azure_store_names(subscription)
    groups.append(store_group)
    if not settings["environment"]:
        groups.append(AZURE_SHARED_GROUP)
    for group_name in groups:
        if az("group", "exists", "--name", group_name) is not True:
            continue
        group = az("group", "show", "--name", group_name)
        found.append(Resource("resource group", group["id"], group.get("tags") or {},
                              group["name"]))
        for item in az("resource", "list", "--resource-group", group_name) or []:
            found.append(Resource(item["type"], item["id"], item.get("tags") or {},
                                  item["name"]))
    return found


def google_cloud_inventory(settings: dict[str, str]) -> Inventory:
    project, region = settings["project"], settings["region"]
    found = []
    assets = gcloud("asset", "search-all-resources",
                    f"--scope=projects/{project}",
                    "--asset-types=" + ",".join(GOOGLE_ASSET_TYPES)) or []
    for item in assets:
        name = str(item.get("displayName") or item["name"]).rsplit("/", 1)[-1]
        if not has_prefix(name):
            continue
        found.append(Resource(item["assetType"], item["name"],
                              dict(item.get("labels") or {}), name))
    jobs = gcloud("scheduler", "jobs", "list",
                  "--location", region, "--project", project) or []
    for item in jobs:
        name = str(item["name"]).rsplit("/", 1)[-1]
        if not has_prefix(name):
            continue
        # A Cloud Scheduler job takes no labels, so pdt marks it by description.
        managed = str(item.get("description") or "").startswith("Managed by PDT")
        found.append(Resource("cloudscheduler.googleapis.com/Job", item["name"],
                              dict(MANAGED) if managed else {}, name))
    # The account this run signs in with may share the pdt prefix, and it is
    # not something deploy made.
    own = gcloud("config", "get-value", "account") or ""
    accounts = gcloud("iam", "service-accounts", "list", "--project", project) or []
    for item in accounts:
        name = str(item["email"]).split("@", 1)[0]
        if not has_prefix(name) or item["email"] == own:
            continue
        # A service account takes no labels, so pdt marks it by display name.
        managed = item.get("displayName") == f"{resource_prefix()} job runner"
        found.append(Resource("iam.googleapis.com/ServiceAccount", item["name"],
                              dict(MANAGED) if managed else {}, name))
    return found


def windows_inventory(_settings: dict[str, str]) -> Inventory:
    tasks = run_json([
        "powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive",
        "-ExecutionPolicy", "Bypass", "-Command", WINDOWS_TASKS])
    if isinstance(tasks, dict):
        tasks = [tasks]
    found = []
    for item in tasks or []:
        name = item["TaskName"]
        if not has_prefix(name):
            continue
        # deploy_windows.py writes this prefix; a task carries no other marker.
        managed = str(item.get("Description") or "").startswith("Managed by pdt;")
        found.append(Resource("scheduled task", f"{item.get('TaskPath') or ''}{name}",
                              dict(MANAGED) if managed else {}, name))
    return found


def timestamp(text: str) -> datetime.datetime:
    stamp = datetime.datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=datetime.timezone.utc)
    return stamp


def aws_runs(settings: dict[str, str], app: str, since: datetime.datetime) -> list[str]:
    try:
        listed = aws(settings["region"], "logs", "filter-log-events",
                     "--log-group-name", f"/ecs/{resource_name(app)}",
                     "--start-time", str(int(since.timestamp() * 1000))) or {}
    except InventoryError as error:
        if "ResourceNotFoundException" not in str(error):
            raise
        return []
    return sorted({event["logStreamName"] for event in listed.get("events") or []
                   if GREETING in str(event.get("message") or "")})


def azure_runs(settings: dict[str, str], app: str, since: datetime.datetime) -> list[str]:
    listed = az("containerapp", "job", "execution", "list", "--name", azure_job_name(app),
                "--resource-group", settings["resource_group"]) or []
    found = []
    for item in listed:
        props = item.get("properties") or {}
        if (props.get("status") == "Succeeded" and props.get("startTime")
                and timestamp(props["startTime"]) >= since):
            found.append(item["name"])
    return found


def google_cloud_runs(settings: dict[str, str], app: str, since: datetime.datetime) -> list[str]:
    listed = gcloud("run", "jobs", "executions", "list", "--job", resource_name(app),
                    "--region", settings["region"], "--project", settings["project"]) or []
    found = []
    for item in listed:
        meta = item.get("metadata") or {}
        created = meta.get("creationTimestamp")
        if ((item.get("status") or {}).get("succeededCount") or 0) >= 1 and created \
                and timestamp(created) >= since:
            found.append(meta["name"])
    return found


def windows_runs(_settings: dict[str, str], app: str, since: datetime.datetime) -> list[str]:
    info = run_json([
        "powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive",
        "-ExecutionPolicy", "Bypass", "-Command",
        WINDOWS_TASK_INFO.format(task=resource_name(app))]) or {}
    last = info.get("LastRunTime")
    if info.get("LastTaskResult") == 0 and last and timestamp(last) >= since:
        return [last]
    return []


RUNS = {
    "aws": aws_runs,
    "azure": azure_runs,
    "google-cloud": google_cloud_runs,
    "windows": windows_runs,
}

INVENTORIES = {
    "aws": aws_inventory,
    "azure": azure_inventory,
    "google-cloud": google_cloud_inventory,
    "windows": windows_inventory,
}

# Each entry reads the same places the matching provider module reads.
SETTINGS = {
    "aws": lambda platform: {
        "region": platform.get("region") or os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION") or "",
    },
    "azure": lambda platform: {
        "subscription": platform.get("subscription")
        or os.environ.get("PDT_AZURE_SUBSCRIPTION") or "",
        "resource_group": platform.get("resource_group")
        or os.environ.get("PDT_AZURE_RESOURCE_GROUP") or resource_prefix(),
        "environment": platform.get("environment")
        or os.environ.get("PDT_AZURE_CONTAINER_APPS_ENVIRONMENT") or "",
    },
    "google-cloud": lambda platform: {
        "project": platform.get("project")
        or os.environ.get("PDT_GOOGLE_CLOUD_PROJECT") or "",
        "region": platform.get("region")
        or os.environ.get("PDT_GOOGLE_CLOUD_REGION") or "us-central1",
    },
    "windows": lambda _platform: {},
}
