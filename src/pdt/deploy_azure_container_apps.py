"""Deploy an app as a scheduled Azure Container Apps Job.

Entered through
deploy_azure.py, which owns the uv script header, login, and Key Vault.
The resource group, ACR, Key Vault, and user-assigned identity are shared
by the project's apps. The Container Apps environment and its Log
Analytics workspace are shared by every pdt project in the subscription,
one environment per region, unless the user names an environment of their
own. Each app owns one tagged job, named pdt-<app>-<project suffix>:
job names are unique across the shared environment, so the suffix keeps
the same app in two resource groups of one subscription apart. A job
named the old way, pdt-<app>, is deleted on the next deploy of its app.

The shared identity pulls the image and reads the app's Key Vault
secret. Each job's own system-assigned identity holds the grants that
name one app: its secret, and its folder of the data store.
"""

from __future__ import annotations

import dataclasses
import datetime
import json
import os
import re
import shutil
import subprocess

from pdt import config, console, runs_cli
from pdt.deploy import confirm
from pdt.deploy_azure import (
    AZ, ENVIRONMENT_TYPE, RECENT_RUNS, SECRET_ROLE, STORE_ROLE, assign_role, az_json, az_tsv,
    azure_settings, check_shared_names, clean_name, cost_estimate, deployer_store,
    destroy_group, disable_old_secret_versions, ensure_group_and_vault, ensure_secret, ensure_shared_group,
    ensure_store, ensure_workspace, group_can_be_deleted, key_vault_item,
    managed_by_pdt, managed_secret, other_pdt_apps, owned_by, preflight,
    purge_secret, report_shared_kept, require_managed, resource_id, retail_price,
    revoke_role, run_basis, run_quiet, run_stream, secret_actions, secret_name,
    secret_scope, secret_state, store_condition, store_cost, store_description, store_exists,
    store_plan, store_settings, store_url, store_usage, workspace_resource,
)
from pdt.deploy_common import (
    CostEstimate, fail, gather_secrets, image_action, run_build, run_secrets,
    stage_build_context, store_kept_line, warn_if_locked, write_dockerfile)

PROVIDERS = ("Microsoft.App", "Microsoft.ContainerRegistry",
             "Microsoft.OperationalInsights")
CPU = "0.5"
MEMORY = "1.0Gi"
QUOTA_HINT = (
    "This subscription's quota of Container Apps environments is used up. pdt "
    "shares one environment between every pdt project in a subscription and "
    "region.\n"
    "Either set platform.environment: <resource-group>/<name> in pdt.yml to an "
    "environment you already have, or request a quota increase for Container "
    "Apps environments in the Azure portal (Subscriptions > Usage + quotas).")


def build_image(app: dict, registry: str, image_name: str) -> None:
    stage = stage_build_context(app)
    try:
        write_dockerfile(stage, app)
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


def set_job_secret(job: str, rg: str, secret_uri: str, identity_id: str) -> None:
    run_quiet(
        "containerapp", "job", "secret", "set", "--name", job,
        "--resource-group", rg, "--secrets",
        f"pdt-env=keyvaultref:{secret_uri},identityref:{identity_id}",
        retry_access=True)


def reconcile_job(settings: dict[str, str], job: str, image: str, cron: str,
                  identity_id: str, secret_uri: str | None, storage_url: str | None,
                  exists: bool, app_name: str) -> None:
    rg = settings["resource_group"]
    common = [
        "--name", job, "--resource-group", rg, "--image", image,
        "--cron-expression", cron, "--cpu", CPU, "--memory", MEMORY,
        "--replica-timeout", "1800", "--replica-retry-limit", "0",
        "--parallelism", "1", "--replica-completion-count", "1",
        "--tags", "managed-by=pdt", f"pdt-app={app_name}",
    ]
    env_vars = []
    if secret_uri:
        env_vars.append("PDT_ENV_JSON=secretref:pdt-env")
        env_vars.append(f"PDT_ENV_SECRET_RESOURCE={secret_uri}")
    if storage_url:
        env_vars.append(f"PDT_STORAGE_URL={storage_url}")
    if not exists:
        args = [
            "containerapp", "job", "create", *common,
            "--environment", settings["environment"].resource_id(settings["subscription"]),
            "--trigger-type", "Schedule",
            "--mi-system-assigned", "--mi-user-assigned", identity_id,
            "--registry-server", f"{settings['registry']}.azurecr.io",
            "--registry-identity", identity_id,
        ]
        if secret_uri:
            args += [
                "--secrets",
                f"pdt-env=keyvaultref:{secret_uri},identityref:{identity_id}",
            ]
        if env_vars:
            args += ["--env-vars", *env_vars]
        run_quiet(*args, retry_access=True, retry_internal=True)
        return

    run_quiet("containerapp", "job", "identity", "assign", "--name", job,
              "--resource-group", rg, "--system-assigned", "--user-assigned", identity_id)
    run_quiet("containerapp", "job", "registry", "set", "--name", job,
              "--resource-group", rg,
              "--server", f"{settings['registry']}.azurecr.io",
              "--identity", identity_id)
    if secret_uri:
        set_job_secret(job, rg, secret_uri, identity_id)
    if env_vars:
        common += ["--replace-env-vars", *env_vars]
    else:
        common += ["--remove-env-vars", "PDT_ENV_JSON", "PDT_ENV_SECRET_RESOURCE", "PDT_STORAGE_URL"]
    run_quiet("containerapp", "job", "update", *common, retry_access=True)
    if not secret_uri:
        # Ignore absence: Azure returns nonzero when there is nothing to remove.
        subprocess.run(
            [*AZ, "containerapp", "job", "secret", "remove", "--name", job,
             "--resource-group", rg, "--secret-names", "pdt-env"],
            stdin=subprocess.DEVNULL, capture_output=True, text=True)


def job_principal_id(job: str, rg: str) -> str:
    current = az_json("containerapp", "job", "show", "--name", job,
                      "--resource-group", rg)
    principal = ((current or {}).get("identity") or {}).get("principalId")
    if not principal:
        fail(f"could not read the identity of Container Apps Job {job}")
    return principal


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


LOG_ANALYTICS_API = "https://api.loganalytics.io"
EXIT_CODE = re.compile(r"exit code '(\d+)'")
RUN_STATUS = {"Succeeded": "succeeded", "Running": "running", "Processing": "running"}


def execution_run(execution: dict) -> runs_cli.Run:
    props = execution.get("properties") or {}
    started = datetime.datetime.fromisoformat(props["startTime"])
    end = props.get("endTime")
    ended = datetime.datetime.fromisoformat(end) if end else None
    status = RUN_STATUS.get(str(props.get("status") or ""), "failed")
    return runs_cli.Run(str(execution.get("name") or ""), started, ended, status)


def list_runs(settings: dict, job: str) -> list[runs_cli.Run]:
    execs = az_json("containerapp", "job", "execution", "list", "--name", job,
                    "--resource-group", settings["resource_group"]) or []
    found = [execution_run(execution) for execution in execs]
    found.sort(key=lambda run: run.started, reverse=True)
    if not found:
        return found
    # The platform's own record: "Container 'x' was terminated with exit code '1' and reason ...".
    rows = log_query(settings, f"ContainerAppSystemLogs_CL | where JobName_s == '{job}' "
                               "and Reason_s == 'ContainerTerminated' "
                               "| project ExecutionName_s, Log_s")
    codes = {}
    for execution, log in rows:
        match = EXIT_CODE.search(log)
        if match:
            codes[execution] = int(match.group(1))
    for run in found:
        run.exit_code = codes.get(run.id)
    return found


def log_query(settings: dict, query: str) -> list[list]:
    workspace_id = az_tsv("monitor", "log-analytics", "workspace", "show",
                          "--resource-group", settings["environment"].resource_group,
                          "--workspace-name", settings["workspace"], "--query", "customerId")
    # `az monitor log-analytics query` needs an extension that pip cannot install
    # into pdt's uv environment, so this calls the query API directly.
    result = az_json("rest", "--method", "post", "--resource", LOG_ANALYTICS_API,
                     "--url", f"{LOG_ANALYTICS_API}/v1/workspaces/{workspace_id}/query",
                     "--body", json.dumps({"query": query})) or {}
    return result.get("tables", [{}])[0].get("rows", [])


def read_lines(settings: dict, job: str, execution: str) -> list[runs_cli.Line]:
    rows = log_query(settings, f"ContainerAppConsoleLogs_CL | where ContainerJobName_s == '{job}' "
                               f"and ContainerGroupName_s startswith '{execution}' "
                               "| project TimeGenerated, Log_s | order by TimeGenerated asc")
    return [runs_cli.parse_line(log, datetime.datetime.fromisoformat(generated))
            for generated, log in rows]


def runs(app: dict, settings: dict, rest: list[str]) -> int:
    job = job_name(settings, app["name"])
    return runs_cli.runs(lambda: list_runs(settings, job), app["name"], rest)


def logs(app: dict, settings: dict, rest: list[str]) -> int:
    job = job_name(settings, app["name"])

    def read(run: runs_cli.Run) -> list[runs_cli.Line]:
        lines = read_lines(settings, job, run.id)
        if not lines and run.ended is not None:
            console.note("Azure Log Analytics receives lines 2 to 5 minutes after a run "
                         "finishes; run pdt logs again in a moment.")
        return lines

    return runs_cli.logs(lambda: list_runs(settings, job), read, app["name"], rest)


def cost_estimate_for(region: str, cron: str, job: str, rg: str,
               job_exists: bool, num_secrets: int,
               usage: tuple[int, int] | None) -> CostEstimate:
    console.status("Fetching list prices from the Azure Retail Prices API...")
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
        if usage is not None:
            items.append(store_cost(usage, region))
    except Exception as exc:
        fail(f"could not calculate the required monthly cost estimate: {exc}")
    return cost_estimate(
        region, items, "excludes ACR image builds/storage and Log Analytics ingestion")


def environment_resource(settings: dict) -> dict | None:
    environment = settings["environment"]
    return az_json("containerapp", "env", "show", "--name", environment.name,
                   "--resource-group", environment.resource_group)


def check_environment(settings: dict, resource: dict | None) -> None:
    environment = settings["environment"]
    if environment.managed:
        require_managed(resource, f"Container Apps environment {environment}")
        return
    if resource is None:
        fail(f"the Container Apps environment {environment} named by "
             f"platform.environment in {config.PROJECT_FILE} does not exist in "
             f"subscription {settings['subscription']}")
    location = str(resource.get("location") or "").lower().replace(" ", "")
    if location and location != settings["region"]:
        fail(f"the Container Apps environment {environment} is in {location}, but "
             f"platform.region in {config.PROJECT_FILE} is {settings['region']}. A "
             "job must run in its environment's region; change one of them")


def environment_actions(settings: dict, exists: bool, logs_exist: bool,
                        shared_group_exists: bool) -> list[str]:
    environment = settings["environment"]
    if not environment.managed:
        return [f"use your own Container Apps environment {environment}"]
    shared = "(shared by every pdt project in this subscription)"
    return [
        ("use existing" if shared_group_exists else "create")
        + f" resource group {environment.resource_group} {shared}",
        ("use existing" if logs_exist else "create")
        + f" Log Analytics workspace {settings['workspace']} in {environment.resource_group}",
        ("use existing" if exists else "create")
        + f" Container Apps environment {environment} {shared}",
    ]


def ensure_environment(settings: dict, exists: bool, logs_exist: bool) -> None:
    environment = settings["environment"]
    if not environment.managed:
        return
    ensure_shared_group(settings)
    logs_id, logs_key = ensure_workspace(settings, logs_exist)
    if exists:
        return
    console.step(f"creating Container Apps environment {environment}")
    run_quiet("containerapp", "env", "create", "--name", environment.name,
              "--resource-group", environment.resource_group,
              "--location", settings["region"],
              "--logs-workspace-id", logs_id, "--logs-workspace-key", logs_key,
              "--tags", "managed-by=pdt",
              hints={"EnvironmentsInSubExceeded": QUOTA_HINT})


def job_name(settings: dict[str, str], app_name: str) -> str:
    suffix = settings["suffix"][:7]
    return f"{clean_name(f'pdt-{app_name}', 32 - len(suffix) - 1)}-{suffix}"


def legacy_job_name(app_name: str) -> str:
    return clean_name(f"pdt-{app_name}")


def find_job(settings: dict[str, str], app_name: str) -> tuple[str, dict | None]:
    """The app's job under its current name, else under the name pdt used before the suffix."""
    job = job_name(settings, app_name)
    current = az_json("containerapp", "job", "show", "--name", job,
                      "--resource-group", settings["resource_group"])
    if current is None:
        legacy = legacy_job_name(app_name)
        old = az_json("containerapp", "job", "show", "--name", legacy,
                      "--resource-group", settings["resource_group"])
        if owned_by(old, app_name):
            return legacy, old
    return job, current


def retire_job(settings: dict[str, str], job: str, current: dict, store: dict | None,
               sid: str) -> None:
    """Delete a job that is about to be recreated under a new name."""
    principal = (current.get("identity") or {}).get("principalId")
    if principal:
        if store:
            revoke_role(store["container_id"], principal, STORE_ROLE)
        revoke_role(secret_scope(settings, sid), principal, SECRET_ROLE)
    console.step(f"deleting Container Apps Job {job}")
    run_quiet("containerapp", "job", "delete", "--name", job,
              "--resource-group", settings["resource_group"], "--yes")


def secrets(app: dict, action: str, assume_yes: bool, name: str | None = None) -> int:
    settings = preflight(app, azure_settings(app))
    name = app["name"]
    job, _current = find_job(settings, name)
    sid = secret_name(name)
    _vault_exists, current = secret_state(settings, sid, name, True)

    def write(values: dict[str, str]) -> None:
        payload = json.dumps(values, sort_keys=True)
        secret_uri = ensure_secret(settings, sid, values, payload, current, name)
        identity_id = az_tsv("identity", "show", "--name", settings["identity"],
                             "--resource-group", settings["resource_group"], "--query", "id")
        set_job_secret(job, settings["resource_group"], secret_uri, identity_id)
        disable_old_secret_versions(settings, sid)

    return run_secrets(action, app, current, write, assume_yes, name)


def deploy(app: dict, assume_yes: bool) -> int:
    settings = preflight(app, azure_settings(app))
    name = app["name"]
    job = job_name(settings, name)
    cron = config.cron_expression(app["schedule"])
    values = gather_secrets(app)
    payload = json.dumps(values, sort_keys=True)
    sid = secret_name(name)
    rg = settings["resource_group"]

    console.status(f"Checking current state in Azure subscription {settings['subscription']} "
          f"({settings['region']})...")
    check_shared_names(settings)
    group = az_json("group", "show", "--name", rg)
    require_managed(group, f"resource group {rg}")
    group_exists = group is not None
    registry = az_json("acr", "show", "--name", settings["registry"],
                       "--resource-group", rg)
    require_managed(registry, f"ACR {settings['registry']}")
    registry_exists = registry is not None
    environment = environment_resource(settings)
    check_environment(settings, environment)
    environment_exists = environment is not None
    identity = az_json("identity", "show", "--name", settings["identity"],
                       "--resource-group", rg)
    require_managed(identity, f"managed identity {settings['identity']}")
    logs_exist = shared_group_exists = False
    if settings["environment"].managed:
        shared_group = az_json("group", "show", "--name", settings["environment"].resource_group)
        require_managed(shared_group, f"resource group {settings['environment'].resource_group}")
        shared_group_exists = shared_group is not None
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
    legacy = legacy_job_name(name)
    legacy_job = None
    if current_job is None and legacy != job:
        legacy_job = az_json("containerapp", "job", "show", "--name", legacy,
                             "--resource-group", rg)
        if not owned_by(legacy_job, name):
            legacy_job = None
    vault_exists, current_secret = secret_state(settings, sid, name, bool(values))
    store = store_settings(settings) if app["storage"] else None
    store_present = store_exists(store) if store else False
    deployer = deployer_store(settings, name) if store_present else None
    usage = (store_usage(deployer) if deployer else (0, 0)) if store else None

    actions = ["register required Azure resource providers"]
    actions += environment_actions(settings, environment_exists, logs_exist,
                                   shared_group_exists)
    actions.append(("use existing" if group_exists else "create")
                   + f" resource group {rg}")
    actions.append(("use existing" if registry_exists else "create")
                   + f" ACR {settings['registry']} (Basic)")
    actions.append(
        ("keep" if arm_auth_enabled else "enable")
        + f" ACR authentication-as-arm on {settings['registry']} "
        "(required for managed-identity image pulls)")
    actions.append(("use existing" if identity else "create")
                   + f" managed identity {settings['identity']}")
    actions.append(("use existing" if vault_exists else "create")
                   + f" Key Vault {settings['vault']} (RBAC)")
    actions.append("ensure scoped Key Vault secret permissions for the deployer "
                   "and managed identity")
    if store:
        actions += store_plan(store, store_present, name, job)
    actions.append(image_action(
        app, f"build and push image {settings['registry']}.azurecr.io/{name}:latest"))
    actions += secret_actions(sid, values, current_secret, payload)
    if legacy_job:
        actions.append(f"delete Container Apps Job {legacy} (its name is now {job})")
    if values:
        actions.append(f"allow {job} to update its own Key Vault secret {sid}")
    actions.append(("update" if current_job else "create")
                   + f' Container Apps Job {job}: "{cron}" (UTC)')
    if not confirm(actions, assume_yes, cost_estimate_for(
            settings["region"], cron, job, rg, current_job is not None,
            1 if values else 0, usage)):
        console.warn("Aborted; nothing was changed.")
        return 1

    ensure_environment(settings, environment_exists, logs_exist)
    vault_id = ensure_group_and_vault(settings, PROVIDERS, vault_exists)
    if not registry_exists:
        console.step(f"creating ACR {settings['registry']}")
        run_quiet("acr", "create", "--name", settings["registry"],
                  "--resource-group", rg, "--location", settings["region"],
                  "--sku", "Basic", "--admin-enabled", "false",
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
    if store:
        ensure_store(settings, store, store_present)
    console.step(f"enabling ACR authentication-as-arm on {settings['registry']}")
    enable_acr_arm_auth(settings["registry"])

    console.step(f"building image {settings['registry']}.azurecr.io/{name}:latest")
    build_image(app, settings["registry"], name)
    secret_uri = ensure_secret(settings, sid, values, payload, current_secret, name)
    if legacy_job:
        retire_job(settings, legacy, legacy_job, store, sid)
    console.step(f"reconciling Container Apps Job {job}")
    image = f"{settings['registry']}.azurecr.io/{name}:latest"
    try:
        reconcile_job(settings, job, image, cron, identity_id, secret_uri,
                      store_url(store, name) if store else None,
                      current_job is not None, name)
    except SystemExit:
        report_job_failure(settings, job)
        raise
    principal = job_principal_id(job, rg)
    if secret_uri:
        disable_old_secret_versions(settings, sid)
        assign_role(secret_scope(settings, sid), principal, SECRET_ROLE)
    if store:
        assign_role(store["container_id"], principal, STORE_ROLE,
                    condition=store_condition(name))
    console.done(f"Deployed {name}.")
    console.field("Run it once", f"pdt az containerapp job start --name {job} --resource-group {rg}")
    console.field("Run logs (Execution history tab)", job_history_url(settings, job))
    return 0


def environment_users(settings: dict) -> dict[str, int]:
    """Jobs outside this project's group that run in the environment, by group."""
    wanted = settings["environment"].resource_id(settings["subscription"]).lower()
    users: dict[str, int] = {}
    for job in az_json("containerapp", "job", "list") or []:
        group = str(job.get("resourceGroup") or "")
        if group.lower() == settings["resource_group"].lower():
            continue
        if str((job.get("properties") or {}).get("environmentId") or "").lower() == wanted:
            users[group] = users.get(group, 0) + 1
    return users


def other_environments(settings: dict) -> list[str]:
    environment = settings["environment"]
    return sorted(
        str(item.get("name")) for item in az_json(
            "resource", "list", "--resource-group", environment.resource_group,
            "--resource-type", ENVIRONMENT_TYPE) or []
        if str(item.get("name")) != environment.name)


@dataclasses.dataclass
class Release:
    """What destroy may let go of after the last app in the project is gone."""

    environment: bool = False
    group: bool = False
    note: str = ""


def environment_release(settings: dict) -> Release:
    environment = settings["environment"]
    if not environment.managed:
        return Release(note=f"Container Apps environment {environment} is your own; "
                            "pdt leaves it as it is")
    resource = environment_resource(settings)
    if resource is None:
        group = az_json("group", "show", "--name", environment.resource_group)
        return Release(group=managed_by_pdt(group) and not other_environments(settings))
    if not managed_by_pdt(resource):
        return Release()
    users = environment_users(settings)
    if users:
        count = sum(users.values())
        return Release(note=f"Container Apps environment {environment} still runs {count} "
                            f"job{'s' if count != 1 else ''} in resource group"
                            f"{'s' if len(users) != 1 else ''} {', '.join(sorted(users))}; "
                            "keeping it")
    return Release(environment=True, group=not other_environments(settings))


def release_actions(settings: dict, release: Release) -> list[str]:
    environment = settings["environment"]
    actions = []
    if release.environment:
        actions.append(f"delete Container Apps environment {environment} (no other job uses it)")
    if release.group:
        actions.append(f"delete resource group {environment.resource_group} and the Log "
                       f"Analytics workspace {settings['workspace']} in it")
    return actions


def release_environment(settings: dict, release: Release) -> None:
    environment = settings["environment"]
    if release.environment:
        console.step(f"deleting Container Apps environment {environment}")
        run_quiet("containerapp", "env", "delete", "--name", environment.name,
                  "--resource-group", environment.resource_group, "--yes")
    if release.group:
        console.step(f"deleting resource group {environment.resource_group}")
        run_quiet("group", "delete", "--name", environment.resource_group, "--yes")


def kept_line(store: dict[str, str], deployer, name: str) -> str:
    usage = store_usage(deployer)
    if usage is None:
        return f"kept: {store_description(store)} (data under {name}/)"
    return store_kept_line(store_description(store), usage[0], name)


def destroy(app: dict, assume_yes: bool) -> int:
    settings = preflight(app, azure_settings(app))
    name = app["name"]
    job, current_job = find_job(settings, name)
    sid = secret_name(name)
    rg = settings["resource_group"]
    managed_job = owned_by(current_job, name)
    secret_owned = managed_secret(settings, sid, name)
    if current_job and not managed_job:
        console.note(f"Container Apps Job {job} is not owned by this app; keeping it")
    store = store_settings(settings) if app["storage"] else None
    store_present = store_exists(store) if store else False
    deployer = deployer_store(settings, name) if store_present else None
    principal_id = ((current_job or {}).get("identity") or {}).get("principalId")
    grant = ""
    if deployer and managed_job and principal_id:
        grant = (f"revoke {job}'s write access to {name}/ "
                 f"in {store_description(store)}")
        warn_if_locked(deployer, name)
    others = other_pdt_apps(rg, name)
    # A run that stopped after the project group went may still owe the
    # environment, so a missing group takes the same path as a deletable one.
    group_exists = az_tsv("group", "exists", "--name", rg) == "true"
    if not group_exists or group_can_be_deleted(settings, others):
        release = environment_release(settings)
        actions = []
        if grant:
            actions.append(grant)
        if group_exists:
            actions += [
                f"delete resource group {rg} and everything in it: the job and the "
                "shared ACR (with images), managed identity, and Key Vault",
                f"purge the soft-deleted Key Vault {settings['vault']}",
            ]
        actions += release_actions(settings, release)
        if release.note:
            console.note(release.note)
        if not actions:
            console.done(f"Nothing owned by {name} to remove.")
            return 0
        if not confirm(actions, assume_yes):
            console.warn("Aborted; nothing was changed.")
            return 1
        if grant:
            revoke_role(store["container_id"], principal_id, STORE_ROLE)
        if group_exists:
            destroy_group(settings)
        release_environment(settings, release)
        if deployer:
            console.say(kept_line(store, deployer, name))
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
    if grant:
        actions.append(grant)
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
    if grant:
        revoke_role(store["container_id"], principal_id, STORE_ROLE)
    if secret_owned and managed_job and principal_id:
        revoke_role(secret_scope(settings, sid), principal_id, SECRET_ROLE)
    if managed_job:
        run_quiet("containerapp", "job", "delete", "--name", job,
                  "--resource-group", rg, "--yes")
    if image_exists:
        run_quiet("acr", "repository", "delete", "--name", settings["registry"],
                  "--repository", name, "--yes")
    if secret_owned:
        purge_secret(settings, sid)
    report_shared_kept(rg, others)
    if deployer:
        console.say(kept_line(store, deployer, name))
    return 0
