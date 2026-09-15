"""Deploy an app as a scheduled Azure Container Apps Job.

Selected with platform.runtime: container_apps. Entered through
deploy_azure.py, which owns the uv script header, login, and Key Vault.
The resource group, Container Apps environment, ACR, Key Vault, and
user-assigned identity are shared. Each app owns one tagged job.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import shutil
import subprocess

from pdt import config, console
from pdt.deploy import confirm
from pdt.deploy_azure import (
    AZ, RECENT_RUNS, assign_role, az_json, az_tsv, azure_settings, check_shared_names,
    clean_name, cost_estimate_lines, destroy_group, ensure_group_and_vault, ensure_secret,
    group_can_be_deleted, key_vault_item, managed_by_pdt, managed_secret,
    other_pdt_apps, owned_by, preflight, purge_secret, report_shared_kept,
    require_managed, resource_id, retail_price, run_basis, run_quiet, run_stream,
    secret_actions, secret_name, secret_state, workspace_resource,
)
from pdt.deploy_common import DOCKERFILE, fail, gather_secrets, run_build, stage_build_context

PROVIDERS = ("Microsoft.App", "Microsoft.ContainerRegistry",
             "Microsoft.OperationalInsights")
CPU = "0.5"
MEMORY = "1.0Gi"


def build_image(app: dict, registry: str, image_name: str) -> None:
    stage = stage_build_context(app)
    try:
        (stage / "Dockerfile").write_text(DOCKERFILE.format(app=app["name"]))
        if os.environ.get("GITLAB_CI") == "true":
            image = f"{registry}.azurecr.io/{image_name}:latest"
            run_quiet("acr", "login", "--name", registry)
            run_build(["docker", "build", "--platform", "linux/amd64",
                       "-t", image, str(stage)])
            run_build(["docker", "push", image])
        else:
            run_stream("acr", "build", "--registry", registry, "--image",
                       f"{image_name}:latest", str(stage))
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def acr_arm_auth_enabled(registry: str) -> bool:
    data = az_json("acr", "config", "authentication-as-arm", "show",
                   "--registry", registry)
    if not isinstance(data, dict):
        return False
    return str(data.get("status") or "").lower() == "enabled"


def enable_acr_arm_auth(registry: str) -> None:
    # Container Apps managed-identity pulls require ACR ARM audience tokens.
    run_quiet("acr", "config", "authentication-as-arm", "update",
              "--registry", registry, "--status", "enabled")


def job_history_url(settings: dict[str, str], job: str) -> str:
    # The job's portal page; its Execution history tab lists every run, and
    # each run's Console link opens that run's logs. No deeper deep link exists.
    return ("https://portal.azure.com/#resource"
            + resource_id(settings, "Microsoft.App", "jobs", job))


def _activity_error(status_message) -> dict:
    if isinstance(status_message, str):
        try:
            status_message = json.loads(status_message)
        except (TypeError, ValueError):
            return {}
    if not isinstance(status_message, dict):
        return {}
    error = status_message.get("error")
    if not isinstance(error, dict):
        error = status_message
    summary = {key: error[key] for key in ("code", "message")
               if isinstance(error.get(key), str)}
    details = error.get("details")
    if isinstance(details, list):
        summary["details"] = [
            {key: item[key] for key in ("code", "message")
             if isinstance(item, dict) and isinstance(item.get(key), str)}
            for item in details if isinstance(item, dict)
        ]
    return summary


def report_job_failure(settings: dict[str, str], job: str) -> None:
    timestamp = datetime.datetime.now(datetime.UTC).isoformat()
    job_id = resource_id(settings, "Microsoft.App", "jobs", job)
    console.heading("Azure Container Apps failure diagnostics:")
    console.bullet(f"resource group: {settings['resource_group']}")
    console.bullet(f"job: {job}")
    console.bullet(f"resource: {job_id}")
    console.bullet(f"diagnostic time (UTC): {timestamp}")

    try:
        data = az_json(
            "containerapp", "job", "show", "--name", job,
            "--resource-group", settings["resource_group"], "--subscription",
            settings["subscription"], "--query",
            "{id:id,name:name,provisioningState:properties.provisioningState,"
            "environmentId:properties.environmentId,identityType:identity.type,"
            "userAssignedIdentities:identity.userAssignedIdentities,tags:tags}")
    except Exception:
        data = None
    if not isinstance(data, dict):
        console.note("job state is unavailable")
    else:
        tags = data.get("tags") or {}
        if not isinstance(tags, dict):
            tags = {}
        tags = {key: tags[key] for key in ("managed-by", "pdt-app")
                if key in tags}
        identities = data.get("userAssignedIdentities") or {}
        if not isinstance(identities, dict):
            identities = {}
        console.bullet(f"provisioning state: {data.get('provisioningState')}")
        console.bullet(f"environment: {data.get('environmentId')}")
        console.bullet(f"identity type: {data.get('identityType')}")
        console.bullet(f"user assigned identities: {list(identities)}")
        console.bullet(f"owned tags: {tags}")

    try:
        events = az_json(
            "monitor", "activity-log", "list", "--resource-group",
            settings["resource_group"], "--subscription", settings["subscription"],
            "--offset", "1h", "--query",
            "[].{eventTimestamp:eventTimestamp,operationName:operationName.value,"
            "status:status.value,subStatus:subStatus.value,correlationId:"
            "correlationId,resourceId:resourceId,statusMessage:properties.statusMessage}")
    except Exception:
        events = None
    if not isinstance(events, list):
        console.note("activity log is unavailable")
        return
    matching = [event for event in events if isinstance(event, dict)
                and str(event.get("resourceId") or "").lower() == job_id.lower()]
    matching.sort(key=lambda event: str(event.get("eventTimestamp") or ""), reverse=True)
    if not matching:
        console.note("no recent activity log events matched this job")
        return
    for event in matching[:5]:
        console.bullet(
            f"activity: {event.get('eventTimestamp')} "
            f"{event.get('operationName')} {event.get('status')} "
            f"{event.get('subStatus')} correlation {event.get('correlationId')} "
            f"error {_activity_error(event.get('statusMessage'))}")


def reconcile_job(settings: dict[str, str], job: str, image: str, cron: str,
                  identity_id: str, secret_uri: str | None,
                  exists: bool, app_name: str) -> None:
    rg = settings["resource_group"]
    common = [
        "--name", job, "--resource-group", rg, "--image", image,
        "--cron-expression", cron, "--cpu", CPU, "--memory", MEMORY,
        "--replica-timeout", "1800", "--replica-retry-limit", "1",
        "--parallelism", "1", "--replica-completion-count", "1",
        "--tags", "managed-by=pdt", f"pdt-app={app_name}",
    ]
    if not exists:
        args = [
            "containerapp", "job", "create", *common,
            "--environment", settings["environment"],
            "--trigger-type", "Schedule",
            "--mi-user-assigned", identity_id,
            "--registry-server", f"{settings['registry']}.azurecr.io",
            "--registry-identity", identity_id,
        ]
        if secret_uri:
            args += [
                "--secrets",
                f"pdt-env=keyvaultref:{secret_uri},identityref:{identity_id}",
                "--env-vars", "PDT_ENV_JSON=secretref:pdt-env",
            ]
        run_quiet(*args, retry_access=True, retry_internal=True)
        return

    run_quiet("containerapp", "job", "identity", "assign", "--name", job,
              "--resource-group", rg, "--user-assigned", identity_id)
    run_quiet("containerapp", "job", "registry", "set", "--name", job,
              "--resource-group", rg,
              "--server", f"{settings['registry']}.azurecr.io",
              "--identity", identity_id)
    if secret_uri:
        run_quiet(
            "containerapp", "job", "secret", "set", "--name", job,
            "--resource-group", rg, "--secrets",
            f"pdt-env=keyvaultref:{secret_uri},identityref:{identity_id}",
            retry_access=True)
        common += ["--replace-env-vars",
                   "PDT_ENV_JSON=secretref:pdt-env"]
    else:
        common += ["--remove-env-vars", "PDT_ENV_JSON"]
    run_quiet("containerapp", "job", "update", *common, retry_access=True)
    if not secret_uri:
        # Ignore absence: Azure returns nonzero when there is nothing to remove.
        subprocess.run(
            [*AZ, "containerapp", "job", "secret", "remove", "--name", job,
             "--resource-group", rg, "--secret-names", "pdt-env"],
            stdin=subprocess.DEVNULL, capture_output=True, text=True)


def average_run_seconds(job: str, rg: str) -> float | None:
    execs = az_json("containerapp", "job", "execution", "list", "--name", job,
                    "--resource-group", rg) or []
    durations = []
    for execution in execs[:RECENT_RUNS]:
        props = execution.get("properties") or {}
        start = props.get("startTime")
        end = props.get("endTime")
        if start and end:
            begun = datetime.datetime.fromisoformat(start)
            done = datetime.datetime.fromisoformat(end)
            durations.append((done - begun).total_seconds())
    if not durations:
        return None
    return sum(durations) / len(durations)


def cost_lines(region: str, cron: str, job: str, rg: str,
               job_exists: bool, num_secrets: int) -> list[str]:
    console.say("Fetching list prices from the Azure Retail Prices API...")
    try:
        runs = config.runs_per_month(cron)
        seconds, basis = run_basis(average_run_seconds(job, rg) if job_exists else None)
        cpu_price, _ = retail_price(region, "Azure Container Apps",
                                    "Standard vCPU Active Usage", "Standard")
        mem_price, _ = retail_price(region, "Azure Container Apps",
                                    "Standard Memory Active Usage", "Standard")
        gib = float(MEMORY.rstrip("Gi"))
        run_cost = runs * seconds * (float(CPU) * cpu_price + gib * mem_price)
        acr_price, _ = retail_price(region, "Container Registry",
                                    "Basic Registry Unit", "Basic")
        items = [
            (f"Container Apps job: ~{runs:.0f} runs x {basis} "
             f"x {float(CPU):g} vCPU / {gib:g} GiB", run_cost),
            ("Container Registry (Basic, shared)", acr_price * 30.44),
        ]
        if num_secrets:
            items.append(key_vault_item(region, runs))
    except Exception as exc:
        fail(f"could not calculate the required monthly cost estimate: {exc}")
    return cost_estimate_lines(
        region, items, "excludes ACR image builds/storage and Log Analytics ingestion")


def deploy(app: dict, assume_yes: bool) -> int:
    settings = preflight(app, azure_settings(app))
    name = app["name"]
    job = clean_name(f"pdt-{name}")
    cron = config.cron_expression(app["schedule"])
    values = gather_secrets(app)
    payload = json.dumps(values, sort_keys=True)
    digest = hashlib.sha256(payload.encode()).hexdigest()
    sid = secret_name(name)
    rg = settings["resource_group"]

    console.say(f"Checking current state in Azure subscription {settings['subscription']} "
          f"({settings['region']})...")
    check_shared_names(settings)
    group = az_json("group", "show", "--name", rg)
    require_managed(group, f"resource group {rg}")
    group_exists = group is not None
    registry = az_json("acr", "show", "--name", settings["registry"],
                       "--resource-group", rg)
    require_managed(registry, f"ACR {settings['registry']}")
    registry_exists = registry is not None
    environment = az_json(
        "containerapp", "env", "show", "--name", settings["environment"],
        "--resource-group", rg)
    require_managed(environment, f"Container Apps environment {settings['environment']}")
    environment_exists = environment is not None
    identity = az_json("identity", "show", "--name", settings["identity"],
                       "--resource-group", rg)
    require_managed(identity, f"managed identity {settings['identity']}")
    workspace = workspace_resource(settings)
    require_managed(workspace, f"Log Analytics workspace {settings['workspace']}")
    logs_exist = workspace is not None
    arm_auth_enabled = (
        acr_arm_auth_enabled(settings["registry"]) if registry_exists else False)
    current_job = az_json("containerapp", "job", "show", "--name", job,
                          "--resource-group", rg)
    if current_job and not owned_by(current_job, name):
        fail(f"Container Apps Job {job} already exists but is not owned by "
             f"PDT app {name}; choose another resource group")
    vault_exists, current_hash = secret_state(settings, sid, name, values)

    actions = ["register required Azure resource providers"]
    actions.append(("use existing" if group_exists else "create")
                   + f" resource group {rg}")
    actions.append(("use existing" if registry_exists else "create")
                   + f" ACR {settings['registry']} (Basic)")
    actions.append(
        ("keep" if arm_auth_enabled else "enable")
        + f" ACR authentication-as-arm on {settings['registry']} "
        "(required for managed-identity image pulls)")
    actions.append(("use existing" if logs_exist else "create")
                   + f" Log Analytics workspace {settings['workspace']} (shared)")
    actions.append(("use existing" if environment_exists else "create")
                   + f" Container Apps environment {settings['environment']}")
    actions.append(("use existing" if identity else "create")
                   + f" managed identity {settings['identity']}")
    actions.append(("use existing" if vault_exists else "create")
                   + f" Key Vault {settings['vault']} (RBAC)")
    actions.append("ensure scoped Key Vault secret permissions for the deployer "
                   "and managed identity")
    actions.append(f"build and push image {settings['registry']}.azurecr.io/{name}:latest")
    actions += secret_actions(sid, values, current_hash, digest)
    actions.append(("update" if current_job else "create")
                   + f' Container Apps Job {job}: "{cron}" (UTC)')
    if not confirm(actions, assume_yes, cost_lines(
            settings["region"], cron, job, rg, current_job is not None, 1 if values else 0)):
        console.warn("Aborted; nothing was changed.")
        return 1

    vault_id = ensure_group_and_vault(settings, PROVIDERS, vault_exists)
    if not registry_exists:
        console.step(f"creating ACR {settings['registry']}")
        run_quiet("acr", "create", "--name", settings["registry"],
                  "--resource-group", rg, "--location", settings["region"],
                  "--sku", "Basic", "--admin-enabled", "false",
                  "--tags", "managed-by=pdt")
    if not environment_exists:
        console.step(f"creating Container Apps environment {settings['environment']}")
        logs_id = az_tsv("monitor", "log-analytics", "workspace", "show",
                         "--resource-group", rg, "--workspace-name",
                         settings["workspace"], "--query", "customerId")
        logs_key = az_tsv("monitor", "log-analytics", "workspace", "get-shared-keys",
                          "--resource-group", rg, "--workspace-name",
                          settings["workspace"], "--query", "primarySharedKey")
        run_quiet("containerapp", "env", "create", "--name", settings["environment"],
                  "--resource-group", rg, "--location", settings["region"],
                  "--logs-workspace-id", logs_id, "--logs-workspace-key", logs_key,
                  "--tags", "managed-by=pdt")
    if not identity:
        console.step(f"creating managed identity {settings['identity']}")
        identity = az_json("identity", "create", "--name", settings["identity"],
                           "--resource-group", rg, "--location", settings["region"],
                           "--tags", "managed-by=pdt")
    if not identity:
        fail(f"could not read managed identity {settings['identity']}")
    identity_id = identity["id"]
    principal_id = identity["principalId"]

    acr_id = resource_id(settings, "Microsoft.ContainerRegistry",
                         "registries", settings["registry"])
    assign_role(acr_id, principal_id, "AcrPull")
    assign_role(vault_id, principal_id, "Key Vault Secrets User")
    console.step(f"enabling ACR authentication-as-arm on {settings['registry']}")
    enable_acr_arm_auth(settings["registry"])

    console.step(f"building image {settings['registry']}.azurecr.io/{name}:latest")
    build_image(app, settings["registry"], name)
    secret_uri = ensure_secret(settings, sid, values, payload, digest, current_hash, name)
    console.step(f"reconciling Container Apps Job {job}")
    image = f"{settings['registry']}.azurecr.io/{name}:latest"
    try:
        reconcile_job(settings, job, image, cron, identity_id, secret_uri,
                      current_job is not None, name)
    except SystemExit:
        report_job_failure(settings, job)
        raise
    console.done(f"Deployed {name}.")
    console.say(f"Run it once now: pdt az containerapp job start --name {job} --resource-group {rg}")
    console.say(f"Run logs: {job_history_url(settings, job)} (Execution history tab)")
    return 0


def destroy(app: dict, assume_yes: bool) -> int:
    settings = preflight(app, azure_settings(app))
    name = app["name"]
    job = clean_name(f"pdt-{name}")
    sid = secret_name(name)
    rg = settings["resource_group"]
    current_job = az_json("containerapp", "job", "show", "--name", job,
                          "--resource-group", rg)
    managed_job = owned_by(current_job, name)
    secret_owned = managed_secret(settings, sid, name)
    if current_job and not managed_job:
        console.note(f"Container Apps Job {job} is not owned by this app; keeping it")
    others = other_pdt_apps(rg, name)
    if group_can_be_deleted(settings, others):
        actions = [
            f"delete resource group {rg} and everything in it: the job and the "
            "shared ACR (with images), Container Apps environment, managed "
            "identity, Key Vault, and Log Analytics workspace",
            f"purge the soft-deleted Key Vault {settings['vault']}",
        ]
        if not confirm(actions, assume_yes):
            console.warn("Aborted; nothing was changed.")
            return 1
        destroy_group(settings)
        return 0
    registry = az_json("acr", "show", "--name", settings["registry"],
                       "--resource-group", rg)
    registry_owned = managed_by_pdt(registry)
    if registry is not None and not registry_owned:
        console.note(f"ACR {settings['registry']} is not managed by PDT; keeping its images")
    image_exists = registry_owned and az_json(
        "acr", "repository", "show", "--name", settings["registry"],
        "--repository", name) is not None
    actions = []
    if managed_job:
        actions.append(f"delete Container Apps Job {job}")
    if image_exists:
        actions.append(f"delete image repository {name} from ACR {settings['registry']}")
    if secret_owned:
        actions.append(f"delete and purge Key Vault secret {sid}")
    if not actions:
        console.done(f"Nothing owned by {name} to remove in resource group {rg}.")
        return 0
    if not confirm(actions, assume_yes):
        console.warn("Aborted; nothing was changed.")
        return 1
    if managed_job:
        run_quiet("containerapp", "job", "delete", "--name", job,
                  "--resource-group", rg, "--yes")
    if image_exists:
        run_quiet("acr", "repository", "delete", "--name", settings["registry"],
                  "--repository", name, "--yes")
    if secret_owned:
        purge_secret(settings, sid)
    report_shared_kept(rg, others)
    return 0
