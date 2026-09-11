"""List what a real cloud account holds, one function per provider.

Every cloud listing runs through pdt's own CLI passthroughs (`pdt aws`,
`pdt az`, `pdt gcloud`), so verifying a provider needs nothing installed
that deploying it does not already install. The Windows provider deploys
to the runner itself, so it reads Task Scheduler through PowerShell.

A listing covers only the names pdt creates, because the same account may
hold resources that have nothing to do with pdt.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field

MANAGED = {"managed-by": "pdt"}
UNTAGGED = "untagged"
SHARED = "shared"
AWS_LOG_PREFIXES = ("/aws/lambda/pdt-", "/pdt/")
GOOGLE_ASSET_TYPES = (
    "run.googleapis.com/Job",
    "secretmanager.googleapis.com/Secret",
    "artifactregistry.googleapis.com/Repository",
)
WINDOWS_TASKS = (
    "$tasks = @(Get-ScheduledTask -TaskPath '\\' | "
    "Where-Object { $_.TaskName -like 'pdt-*' } | "
    "Select-Object TaskName, TaskPath, Description); "
    "ConvertTo-Json -InputObject $tasks -Compress"
)


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
             if f"pdt-{app}" in resource.id or f"pdt-{app}" in resource.name]
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
        if not item["FunctionName"].startswith("pdt-"):
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
        if not name.startswith("pdt-") or name == own:
            continue
        listed = aws(region, "iam", "list-role-tags", "--role-name", name) or {}
        tags = {tag["Key"]: tag["Value"] for tag in listed.get("Tags") or []}
        found.append(Resource("iam role", item["Arn"], tags, name))
    return found


def aws_log_groups(region: str) -> Inventory:
    found = []
    for prefix in AWS_LOG_PREFIXES:
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
        if not item["Name"].startswith("pdt-"):
            continue
        tags = {tag["Key"]: tag["Value"] for tag in item.get("Tags") or []}
        found.append(Resource("secret", item["ARN"], tags, item["Name"]))
    return found


def aws_schedules(region: str) -> Inventory:
    found = []
    listed = aws(region, "scheduler", "list-schedule-groups") or {}
    for group in listed.get("ScheduleGroups") or []:
        if group["Name"] != "pdt":
            continue
        tagged = aws(region, "scheduler", "list-tags-for-resource",
                     "--resource-arn", group["Arn"]) or {}
        tags = {tag["Key"]: tag["Value"] for tag in tagged.get("Tags") or []}
        found.append(Resource("schedule group", group["Arn"], tags, group["Name"]))
        schedules = aws(region, "scheduler", "list-schedules",
                        "--group-name", group["Name"]) or {}
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
        if name != "pdt" or item.get("status") != "ACTIVE":
            continue
        found.append(Resource("ecs cluster", arn, aws_ecs_tags(region, arn), name))
    return found


def aws_task_definitions(region: str) -> Inventory:
    listed = aws(region, "ecs", "list-task-definitions",
                 "--family-prefix", "pdt-", "--status", "ACTIVE") or {}
    return [Resource("ecs task definition", arn, aws_ecs_tags(region, arn),
                     arn.rsplit("/", 1)[-1])
            for arn in listed.get("taskDefinitionArns") or []]


def aws_repositories(region: str) -> Inventory:
    found = []
    for item in (aws(region, "ecr", "describe-repositories") or {}).get("repositories") or []:
        name = item["repositoryName"]
        if name != "pdt" and not name.startswith("pdt-"):
            continue
        arn = item["repositoryArn"]
        listed = aws(region, "ecr", "list-tags-for-resource", "--resource-arn", arn) or {}
        tags = {tag["Key"]: tag["Value"] for tag in listed.get("tags") or []}
        found.append(Resource("ecr repository", arn, tags, name))
    return found


def aws_tagged(region: str) -> Inventory:
    listed = aws(region, "resourcegroupstaggingapi", "get-resources",
                 "--tag-filters", "Key=managed-by,Values=pdt") or {}
    found = []
    for item in listed.get("ResourceTagMappingList") or []:
        arn = item["ResourceARN"]
        # ECS keeps a deleted cluster or task definition visible as INACTIVE,
        # and this API still returns it. The ECS listings above decide those.
        if arn.split(":")[2] == "ecs":
            continue
        tags = {tag["Key"]: tag["Value"] for tag in item.get("Tags") or []}
        found.append(Resource(arn.split(":")[2], arn, tags, arn.rsplit("/", 1)[-1]))
    return found


AWS_SOURCES = (
    aws_functions, aws_roles, aws_log_groups, aws_secrets, aws_schedules,
    aws_clusters, aws_task_definitions, aws_repositories, aws_tagged,
)


def aws_inventory(settings: dict[str, str]) -> Inventory:
    # The tagging API omits an untagged leftover, so the per-kind listings
    # above it decide what exists and it only adds what they do not cover.
    found: dict[str, Resource] = {}
    for source in AWS_SOURCES:
        for resource in source(settings["region"]):
            found.setdefault(resource.id, resource)
    return sorted(found.values(), key=lambda resource: resource.id)


def azure_inventory(settings: dict[str, str]) -> Inventory:
    group_name = settings["resource_group"]
    if az("group", "exists", "--name", group_name) is not True:
        return []
    group = az("group", "show", "--name", group_name)
    found = [Resource("resource group", group["id"], group.get("tags") or {},
                      group["name"])]
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
        if not name.startswith("pdt"):
            continue
        found.append(Resource(item["assetType"], item["name"],
                              dict(item.get("labels") or {}), name))
    jobs = gcloud("scheduler", "jobs", "list",
                  "--location", region, "--project", project) or []
    for item in jobs:
        name = str(item["name"]).rsplit("/", 1)[-1]
        if not name.startswith("pdt"):
            continue
        # A Cloud Scheduler job takes no labels, so pdt marks it by description.
        managed = str(item.get("description") or "").startswith("Managed by PDT")
        found.append(Resource("cloudscheduler.googleapis.com/Job", item["name"],
                              dict(MANAGED) if managed else {}, name))
    accounts = gcloud("iam", "service-accounts", "list", "--project", project) or []
    for item in accounts:
        name = str(item["email"]).split("@", 1)[0]
        if not name.startswith("pdt"):
            continue
        # A service account takes no labels, so pdt marks it by display name.
        managed = item.get("displayName") == "pdt job runner"
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
        # deploy_windows.py writes this prefix; a task carries no other marker.
        managed = str(item.get("Description") or "").startswith("Managed by pdt;")
        found.append(Resource("scheduled task", f"{item.get('TaskPath') or ''}{name}",
                              dict(MANAGED) if managed else {}, name))
    return found


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
        "resource_group": platform.get("resource_group")
        or os.environ.get("PDT_AZURE_RESOURCE_GROUP") or "pdt",
    },
    "google-cloud": lambda platform: {
        "project": platform.get("project")
        or os.environ.get("PDT_GOOGLE_CLOUD_PROJECT") or "",
        "region": platform.get("region")
        or os.environ.get("PDT_GOOGLE_CLOUD_REGION") or "us-central1",
    },
    "windows": lambda _platform: {},
}
