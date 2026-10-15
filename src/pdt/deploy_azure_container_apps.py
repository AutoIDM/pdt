"""Deploy an app as a scheduled Azure Container Apps Job.

Entered through
deploy_azure.py, which owns the uv script header, login, and Key Vault.
The resource group, ACR, Key Vault, and user-assigned identity are shared
by the project's apps. The Container Apps environment and its Log
Analytics workspace are shared by every pdt project in the subscription,
one environment per region, unless the user names an environment of their
own. Each app owns one tagged job, named by `job_name`. A job named the
old way, by `legacy_job_name`, is deleted on the next deploy of its app.

The shared identity pulls the image and reads the app's Key Vault
secret. Each job's own system-assigned identity holds the grants that
name one app: its secret, and its folder of the data store.

Deploy holds a CanNotDelete lock per app on the environment and on the
ACR before the image build (`shared_locks`), so a destroy of another app
that runs meanwhile cannot delete either; Azure refuses the delete while a
lock exists, and destroy reports the refusal as "kept". Destroy removes its
own locks first and re-reads `environment_users` right before it deletes
the environment. The project lock sits on the ACR rather than on the
resource group, because a group lock is inherited by every resource in it
and would stop a sibling app from deleting its own job; a locked ACR still
blocks `az group delete`. The lock on the environment carries the
project's resource group in its name, because two projects can each have
an app of the same name.

When Docker on this computer cannot build the image (`build_in_cloud`), an
ACR Tasks quick build (`az acr build`) builds it from the same staged
context and pushes it to the project's ACR. It needs no Azure resource
beyond the ACR: the build runs on Azure's shared agents, and its run
record and log stay in the registry, so destroy removes them with it.
"""

from __future__ import annotations

import dataclasses
import datetime
import hashlib
import http.client
import json
import re
import shutil
import sys
import time
import urllib.request
from pathlib import Path
from typing import Callable, Iterator

from pdt import config, console, lifecycle, regions, runs_cli
from pdt.deploy import DECLINED, confirm
from pdt.deploy_azure import (
    COMMON_PROVIDERS, ENVIRONMENT_TYPE, RECENT_RUNS, SECRET_ROLE, STORE_ROLE, assign_role, az, az_json, az_tsv,
    azure_settings, check_shared_names, clean_name, cost_estimate, delete_unless_locked, deployer_store,
    destroy_group, disable_old_secret_versions, exchange_rate, ensure_group, ensure_group_and_vault,
    ensure_secret, ensure_store, ensure_workspace, group_can_be_deleted, key_vault_item, list_remaining,
    managed_by_pdt, managed_secret, other_pdt_apps, owned_by, preflight,
    purge_secret, report_shared_kept, require_managed, resource_id, retail_price,
    revoke_role, run_basis, run_quiet, secret_actions, secret_name,
    secret_scope, secret_state, store_condition, store_cost, store_description, store_exists,
    store_plan, store_settings, store_url, store_usage, workspace_keys, workspace_resource,
)
from pdt.deploy_common import (
    CostEstimate, build_in_cloud, convert_from_usd, fail, gather_secrets,
    image_action, run_build, run_secrets, ssh_build_args, stage_build_context, store_kept_line,
    warn_if_locked, write_dockerfile)

PROVIDERS = ("Microsoft.App", "Microsoft.ContainerRegistry",
             "Microsoft.OperationalInsights")
CPU = "0.5"
MEMORY = "1.0Gi"
# A job update takes a few seconds; `az containerapp job update` waits a fixed 15.
JOB_UPDATE_POLLS = 150
JOB_UPDATE_POLL_SECONDS = 2
QUOTA_HINT = (
    "This subscription's quota of Container Apps environments is used up. pdt "
    "shares one environment between every pdt project in a subscription and "
    "region.\n"
    "Either set platform.environment: <resource-group>/<name> in pdt.yml to an "
    "environment you already have, or request a quota increase for Container "
    "Apps environments in the Azure portal (Subscriptions > Usage + quotas).")


def build_image(app: dict, settings: dict, image_name: str, cloud: bool) -> None:
    registry = settings["registry"]
    stage = stage_build_context(app)
    try:
        write_dockerfile(stage, app)
        if cloud:
            cloud_build(settings, stage, image_name)
            return
        image = f"{registry}.azurecr.io/{image_name}:latest"
        login = json.loads(run_quiet("acr", "login", "--name", registry, "--expose-token"))
        run_build(["docker", "login", "--username", "00000000-0000-0000-0000-000000000000",
                   "--password-stdin", login["loginServer"]], data=login["accessToken"],
                  quiet=True)
        run_build(["docker", "build", "--platform", "linux/amd64", *ssh_build_args(),
                   "-t", image, str(stage)])
        run_build(["docker", "push", image])
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def pushed_image(settings: dict, image_name: str) -> str:
    """The image the job runs, by the digest of its linux/amd64 manifest.

    A Docker build pushes an index whose attestation changes on every build, so the
    digest of `:latest` changes with no change to the image; the platform manifest's
    digest changes only with the image. A build that pushes no index has its tag's digest.
    """
    registry = settings["registry"]
    manifest = json.loads(run_quiet("acr", "manifest", "show", "--registry", registry,
                                    "--name", f"{image_name}:latest", "--output", "json"))
    digests = [entry["digest"] for entry in manifest.get("manifests") or []
               if (entry.get("platform") or {}).get("architecture") == "amd64"]
    digest = digests[0] if digests else az_tsv(
        "acr", "repository", "show", "--name", registry, "--image", f"{image_name}:latest",
        "--query", "digest")
    return f"{registry}.azurecr.io/{image_name}@{digest}"


ACR_TASKS_ROLE = "Container Registry Tasks Contributor"
DENIED_ACTION = re.compile(r"perform action '([^']+)'")


def cloud_build(settings: dict, stage: Path, image_name: str) -> None:
    """Build and push the image with an ACR Tasks quick build, showing its log as it runs.

    `az acr build` prints the log with print(). A Windows console or pipe in a legacy code
    page cannot encode every character of it, and the UnicodeEncodeError would stop the
    command while the build goes on (Azure/azure-cli#7011, #22301), so stdout replaces such
    a character for the length of the build. `--no-format` keeps colorama from replacing
    sys.stdout for the rest of the deploy.
    """
    registry, rg = settings["registry"], settings["resource_group"]
    errors = getattr(sys.stdout, "errors", None)
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure:
        reconfigure(errors="replace")
    try:
        proc = az("acr", "build", "--registry", registry, "--resource-group", rg,
                  "--image", f"{image_name}:latest", "--platform", "linux/amd64",
                  "--no-format", str(stage))
    finally:
        if reconfigure:
            reconfigure(errors=errors)
    if proc.returncode == 0:
        return
    error = proc.stderr.strip()
    if "tasksoperationsnotallowed" in error.lower():
        fail(f"Azure does not run ACR Tasks builds in subscription "
             f"{console.value(settings['subscription'])}. Azure pauses ACR Tasks for "
             "subscriptions on free credits, such as Free Trial and Azure for Students. Start "
             "Docker on this computer and run the same command again to build the image there, "
             "or upgrade the subscription to pay-as-you-go in the Azure portal.")
    denied = DENIED_ACTION.search(error)
    if denied or "authorizationfailed" in error.lower():
        acr_id = resource_id(settings, "Microsoft.ContainerRegistry", "registries", registry)
        command = (f"pdt az role assignment create --assignee-object-id "
                   f"{settings['deployer_object_id']} --assignee-principal-type "
                   f"{settings['deployer_principal_type']} --role \"{ACR_TASKS_ROLE}\" "
                   f"--scope {acr_id}")
        fail(f"Your Azure sign-in has no permission to build images with ACR Tasks on ACR "
             f"{console.value(registry)}"
             + (f" (Azure refused {console.value(denied.group(1))})" if denied else "")
             + f". Add the role {console.value(ACR_TASKS_ROLE)} on the registry: "
             f"{console.value(command)}. The command needs the Owner or Role Based Access "
             "Control Administrator role. Or start Docker on this computer and run the same "
             "command again to build the image there.")
    if error:
        console.say(console.escape(error))
    fail(f"{console.value('pdt az acr build')} failed; the build log and the error above say why")


def acr_arm_auth_enabled(registry: dict | None) -> bool:
    """What `az acr config authentication-as-arm show` reads, from the `az acr show` answer."""
    policy = ((registry or {}).get("policies") or {}).get("azureAdAuthenticationAsArmPolicy")
    return str((policy or {}).get("status") or "").lower() == "enabled"


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
    console.bullet(f"resource group: {console.value(settings['resource_group'])}")
    console.bullet(f"job: {console.value(job)}")
    console.bullet(f"resource: {console.value(job_id)}")
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
        console.bullet(console.escape(f"provisioning state: {data.get('provisioningState')}"))
        console.bullet(console.escape(f"environment: {data.get('environmentId')}"))
        console.bullet(console.escape(f"identity type: {data.get('identityType')}"))
        console.bullet(console.escape(f"user assigned identities: {list(identities)}"))
        console.bullet(console.escape(f"owned tags: {tags}"))

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
        console.bullet(console.escape(
            f"activity: {event.get('eventTimestamp')} "
            f"{event.get('operationName')} {event.get('status')} "
            f"{event.get('subStatus')} correlation {event.get('correlationId')} "
            f"error {_activity_error(event.get('statusMessage'))}"))


def set_job_secret(job: str, rg: str, secret_uri: str, identity_id: str) -> None:
    run_quiet(
        "containerapp", "job", "secret", "set", "--name", job,
        "--resource-group", rg, "--secrets",
        f"pdt-env=keyvaultref:{secret_uri},identityref:{identity_id}",
        retry_access=True)


def job_body(registry: str, job: str, image: str, cron: str, identity_id: str,
             secret_uri: str | None, storage_url: str | None, app_name: str) -> dict:
    """The parts of the job resource that deploy sets, as the resource manager API takes them."""
    env = []
    if secret_uri:
        env.append({"name": "PDT_ENV_JSON", "secretRef": "pdt-env"})
        env.append({"name": "PDT_ENV_SECRET_RESOURCE", "value": secret_uri})
    if storage_url:
        env.append({"name": "PDT_STORAGE_URL", "value": storage_url})
    secrets = [{"name": "pdt-env", "keyVaultUrl": secret_uri, "identity": identity_id}]
    return {
        "identity": {"type": "SystemAssigned,UserAssigned",
                     "userAssignedIdentities": {identity_id: {}}},
        "tags": {"managed-by": "pdt", "pdt-app": app_name},
        "properties": {
            "configuration": {
                "triggerType": "Schedule", "replicaTimeout": 1800, "replicaRetryLimit": 0,
                "scheduleTriggerConfig": {"cronExpression": cron, "parallelism": 1,
                                          "replicaCompletionCount": 1},
                "registries": [{"server": f"{registry}.azurecr.io", "identity": identity_id}],
                "secrets": secrets if secret_uri else [],
            },
            "template": {"containers": [{
                "name": job, "image": image, "env": env,
                "resources": {"cpu": float(CPU), "memory": MEMORY},
            }]},
        },
    }


def quantity(value, unit: str = ""):
    """0.5 for "0.5", 1.0 for "1Gi" or "1.0Gi"; any other value as it is."""
    match = re.fullmatch(rf"(\d+(?:\.\d+)?){unit}", str(value))
    return float(match.group(1)) if match else value


def normalized_job(resource: dict) -> dict:
    """What `job_body` sets, read from a job or a body in one form, so equal settings compare equal.

    Azure returns a resource id in another case, "1Gi" for "1.0Gi", 0.5 for "0.5", and
    "SystemAssigned, UserAssigned" for "SystemAssigned,UserAssigned". Any other difference
    compares unequal, so deploy writes the job.
    """
    identity = resource.get("identity") or {}
    properties = resource.get("properties") or {}
    configuration = properties.get("configuration") or {}
    schedule = configuration.get("scheduleTriggerConfig") or {}
    return {
        "identity": sorted(part.strip() for part in (identity.get("type") or "").split(",")),
        "user_identities": sorted(key.lower() for key in identity.get("userAssignedIdentities") or {}),
        "tags": resource.get("tags") or {},
        "trigger": configuration.get("triggerType"),
        "replica_timeout": configuration.get("replicaTimeout"),
        "retry_limit": configuration.get("replicaRetryLimit"),
        "schedule": [schedule.get("cronExpression"), schedule.get("parallelism"),
                     schedule.get("replicaCompletionCount")],
        "registries": [[registry.get("server"), (registry.get("identity") or "").lower()]
                       for registry in configuration.get("registries") or []],
        "secrets": [[secret.get("name"), secret.get("keyVaultUrl"),
                     (secret.get("identity") or "").lower()]
                    for secret in configuration.get("secrets") or []],
        "containers": [{
            "name": container.get("name"),
            "image": container.get("image"),
            "env": [[var.get("name"), var.get("value") or None, var.get("secretRef") or None]
                    for var in container.get("env") or []],
            "cpu": quantity((container.get("resources") or {}).get("cpu")),
            "memory": quantity((container.get("resources") or {}).get("memory"), "Gi"),
        } for container in (properties.get("template") or {}).get("containers") or []],
    }


def reconcile_job(settings: dict[str, str], job: str, image: str, cron: str,
                  identity_id: str, secret_uri: str | None, storage_url: str | None,
                  current: dict | None, app_name: str) -> dict | None:
    """Create or update the job. Return it as Azure now holds it, or None after a create."""
    rg = settings["resource_group"]
    body = job_body(settings["registry"], job, image, cron, identity_id, secret_uri,
                    storage_url, app_name)
    if current is None:
        env = body["properties"]["template"]["containers"][0]["env"]
        args = [
            "containerapp", "job", "create",
            "--name", job, "--resource-group", rg, "--image", image,
            "--cron-expression", cron, "--cpu", CPU, "--memory", MEMORY,
            "--replica-timeout", "1800", "--replica-retry-limit", "0",
            "--parallelism", "1", "--replica-completion-count", "1",
            "--tags", "managed-by=pdt", f"pdt-app={app_name}",
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
        if env:
            args += ["--env-vars", *(f"{var['name']}=secretref:{var['secretRef']}"
                                     if "secretRef" in var else f"{var['name']}={var['value']}"
                                     for var in env)]
        run_quiet(*args, retry_access=True, retry_internal=True)
        return None

    desired = normalized_job(body)
    if normalized_job(current) == desired:
        console.done(f"Container Apps Job {console.value(job)} is unchanged; skipped the update.")
        return current
    run_quiet("rest", "--method", "patch", "--url", job_url(settings, job),
              "--body", json.dumps(body), retry_access=True)
    for _ in range(JOB_UPDATE_POLLS):
        resource = job_resource(settings, job)
        state = (resource.get("properties") or {}).get("provisioningState")
        if state in ("Failed", "Canceled"):
            fail(f"Azure could not update Container Apps Job {console.value(job)} "
                 f"(provisioning state {console.value(state)})")
        if state == "Succeeded" and normalized_job(resource) == desired:
            return resource
        time.sleep(JOB_UPDATE_POLL_SECONDS)
    differ = [key for key, value in normalized_job(resource).items() if value != desired[key]]
    fail(f"Container Apps Job {console.value(job)} did not show the update after "
         f"{console.value(JOB_UPDATE_POLLS * JOB_UPDATE_POLL_SECONDS)} seconds: provisioning state "
         f"{console.value(state)}, settings that differ: {console.value(', '.join(differ))}")


def job_principal_id(job: str, rg: str) -> str:
    current = az_json("containerapp", "job", "show", "--name", job,
                      "--resource-group", rg)
    principal = ((current or {}).get("identity") or {}).get("principalId")
    if not principal:
        fail(f"could not read the identity of Container Apps Job {console.value(job)}")
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
# The CLI has no suspend or resume for a job; the resource manager API does,
# from this api-version on, and it is what reports properties.runningState.
MANAGEMENT_API = "https://management.azure.com"
JOBS_API_VERSION = "2026-07-01"
# A running execution's replicas, and the token for the job's log stream, exist only
# in this preview api-version, the one `az containerapp job logs show` uses.
LOG_STREAM_API_VERSION = "2023-11-02-preview"
# One line of the log stream: the container runtime's time, stdout or stderr, F for a
# whole line or P for part of a long one, and the text the container wrote.
STREAM_LINE = re.compile(r"(\S+Z) (stdout|stderr) ([FP]) ?(.*)")
# A quiet container sends nothing; after this many seconds pdt opens the stream again.
STREAM_TIMEOUT = 60
# The actions the log stream needs. The built-in roles that hold getAuthToken also hold
# listSecrets, which reads the job's secrets
# (https://learn.microsoft.com/en-us/azure/container-apps/jobs#permissions).
STREAM_ACTIONS = ("Microsoft.App/jobs/read", "Microsoft.App/jobs/executions/read",
                  "Microsoft.App/jobs/execution/read", "Microsoft.App/jobs/getAuthToken/action")
# The built-in role, and the one action, that let an account query a workspace
# (https://learn.microsoft.com/en-us/azure/azure-monitor/logs/manage-access).
QUERY_ROLE = "Log Analytics Reader"
QUERY_ACTION = "Microsoft.OperationalInsights/workspaces/query/read"
DENIED_ACTION = re.compile(r"perform action '([^']+)'")
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
    proc = az("rest", "--method", "post", "--resource", LOG_ANALYTICS_API,
              "--url", f"{LOG_ANALYTICS_API}/v1/workspaces/{workspace_id}/query",
              "--body", json.dumps({"query": query}), "--output", "json")
    if proc.returncode != 0:
        if "InsufficientAccessError" in proc.stderr:
            console.warn_once("your Azure account may not query Log Analytics workspace "
                              f"{console.value(settings['workspace'])}; the role "
                              f"{console.value(QUERY_ROLE)} on the workspace allows it, as does "
                              f"{console.value(QUERY_ACTION)}")
        else:
            console.warn_once("Azure Log Analytics did not answer: "
                              f"{console.escape(proc.stderr.strip())}")
        return []
    result = json.loads(proc.stdout or "null") or {}
    return result.get("tables", [{}])[0].get("rows", [])


def read_lines(settings: dict, job: str, execution: str,
               since: datetime.datetime | None = None) -> list[runs_cli.Line]:
    return read_many(settings, job, [execution], since)[execution]


def read_many(settings: dict, job: str, executions: list[str],
              since: datetime.datetime | None = None) -> dict[str, list[runs_cli.Line]]:
    """The lines of several executions from one Log Analytics query, keyed by execution,
    from `since` on.

    A line's time is time_t, the time the container runtime read it from the
    container. TimeGenerated is the time Log Analytics collected it, which one
    batch of lines shares, so it cannot put the lines of a batch in order. A
    workspace that has never had a time_t column has only TimeGenerated."""
    if not executions:
        return {}
    wanted = " or ".join(f"ContainerGroupName_s startswith '{execution}'"
                         for execution in executions)
    after = "" if since is None else (
        f"| where written >= datetime({since.astimezone(datetime.UTC):%Y-%m-%dT%H:%M:%S.%fZ}) ")
    rows = log_query(settings, f"ContainerAppConsoleLogs_CL | where ContainerJobName_s == '{job}' "
                               f"and ({wanted}) | extend written = coalesce("
                               "column_ifexists('time_t', datetime(null)), TimeGenerated) "
                               f"{after}| project written, Log_s, ContainerGroupName_s "
                               "| order by written asc")
    lines = {execution: [] for execution in executions}
    for written, log, group in rows:
        for execution in executions:
            if group.startswith(execution):
                lines[execution].append(
                    runs_cli.parse_line(log, datetime.datetime.fromisoformat(written)))
                break
    return lines


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuses a redirect, so the log stream's token goes to no other host."""

    def redirect_request(self, *args):
        return None


STREAM_OPENER = urllib.request.build_opener(NoRedirect)


def stream_json(job: str, *args: str):
    """What one `az` call for the log stream returns; a refusal or an empty answer is a
    StreamError, which names the actions a custom role needs on an authorization error."""
    proc = az(*args, "--output", "json")
    if proc.returncode == 0:
        answer = json.loads(proc.stdout or "null")
        if answer is None:
            raise runs_cli.StreamError(f"Azure sent an empty answer for the log stream of job {job}")
        return answer
    denied = DENIED_ACTION.search(proc.stderr)
    if denied:
        raise runs_cli.StreamError(
            f"your Azure account may not perform {denied.group(1)} on job {job}; "
            f"a custom role on the job with the actions {', '.join(STREAM_ACTIONS)} allows it")
    raise runs_cli.StreamError(proc.stderr.strip()
                               or f"Azure did not open the log stream of job {job}")


def stream_lines(settings: dict, job: str, run: runs_cli.Run,
                 follow: bool) -> Iterator[runs_cli.Line]:
    """The lines a running execution's container has written, from the job's live log stream.

    With `follow`, the stream stays open and yields each new line until the container
    exits. An execution whose container has not started yet has no lines. The
    container runtime splits a long line into parts; the parts of each of stdout and
    stderr are joined again."""
    url = f"{MANAGEMENT_API}{resource_id(settings, 'Microsoft.App', 'jobs', job)}"
    version = f"api-version={LOG_STREAM_API_VERSION}"
    try:
        replicas = stream_json(job, "rest", "--method", "get",
                               "--url", f"{url}/executions/{run.id}/replicas?{version}")
        if not replicas.get("value"):
            return
        token = stream_json(job, "rest", "--method", "post", "--url", f"{url}/getAuthToken?{version}")
        current = stream_json(job, "rest", "--method", "get", "--url", job_url(settings, job))
        endpoint = current["properties"]["eventStreamEndpoint"]
        container = current["properties"]["template"]["containers"][0]["name"]
        stream_url = (f"{endpoint[:endpoint.index('/subscriptions/')]}"
                      f"/subscriptions/{settings['subscription']}"
                      f"/resourceGroups/{settings['resource_group']}/jobs/{job}"
                      f"/executions/{run.id}/replicas/{replicas['value'][0]['name']}"
                      f"/containers/{container}/logstream?follow={str(follow).lower()}&output=text")
        request = urllib.request.Request(
            stream_url, headers={"Authorization": f"Bearer {token['properties']['token']}"})
        with STREAM_OPENER.open(request, timeout=STREAM_TIMEOUT) as response:
            parts = {"stdout": "", "stderr": ""}
            for raw in response:
                match = STREAM_LINE.fullmatch(raw.decode("utf-8", "replace").rstrip("\r\n"))
                if match is None:
                    continue
                written, output, tag, text = match.groups()
                if tag == "P":
                    parts[output] += text
                    continue
                yield runs_cli.parse_line(parts[output] + text,
                                          datetime.datetime.fromisoformat(written))
                parts[output] = ""
    except TimeoutError:
        return
    except (OSError, http.client.HTTPException) as exc:
        raise runs_cli.StreamError(str(exc)) from exc
    except (LookupError, ValueError) as exc:
        raise runs_cli.StreamError(f"Azure sent a log stream pdt cannot read: {exc!r}") from exc


def runs(app: dict, settings: dict, rest: list[str]) -> int:
    job, _current = find_job(settings, app["name"])
    return runs_cli.runs(lambda: list_runs(settings, job), app["name"], rest)


def job_url(settings: dict, job: str, action: str = "") -> str:
    path = resource_id(settings, "Microsoft.App", "jobs", job)
    return f"{MANAGEMENT_API}{path}{action}?api-version={JOBS_API_VERSION}"


def job_resource(settings: dict, job: str) -> dict:
    return json.loads(run_quiet("rest", "--method", "get", "--url", job_url(settings, job))
                      or "{}")


def job_running_state(settings: dict, job: str) -> str:
    """Ready, Progressing, or Suspended."""
    return str((job_resource(settings, job).get("properties") or {}).get("runningState") or "")


def set_job_paused(settings: dict, job: str, paused: bool, resource: dict | None = None) -> None:
    """`resource` is the job as the resource manager API just returned it, if deploy has it."""
    state = (str((resource.get("properties") or {}).get("runningState") or "") if resource
             else job_running_state(settings, job))
    if (state == "Suspended") == paused:
        return
    run_quiet("rest", "--method", "post",
              "--url", job_url(settings, job, "/suspend" if paused else "/resume"))


def pause(app: dict, settings: dict, paused: bool) -> int:
    job, _current = find_job(settings, app["name"])
    set_job_paused(settings, job, paused)
    console.done(f"{'Paused' if paused else 'Unpaused'} {console.value(app['name'])}: "
                 f"Container Apps Job {console.value(job)} is "
                 f"{'Suspended' if paused else 'Ready'}.")
    return 0


def start(app: dict, settings: dict, follow: bool = False) -> int:
    job, current = find_job(settings, app["name"])
    if current is None:
        fail(f"{console.value(app['name'])} has no Container Apps Job yet; run "
             f"{console.value('pdt deploy ' + app['name'])} first.")
    # A start with an override replaces the container, so it repeats the job's own one.
    container = current["properties"]["template"]["containers"][0]
    env = [f"{var['name']}=secretref:{var['secretRef']}" if "secretRef" in var
           else f"{var['name']}={var.get('value', '')}"
           for var in container.get("env") or [] if var["name"] != lifecycle.TRIGGER_ENV]
    resources = container.get("resources") or {}
    output = run_quiet("containerapp", "job", "start", "--name", job,
                       "--resource-group", settings["resource_group"],
                       "--container-name", container["name"], "--image", container["image"],
                       "--cpu", str(resources.get("cpu", CPU)),
                       "--memory", resources.get("memory", MEMORY),
                       "--env-vars", *env, f"{lifecycle.TRIGGER_ENV}=manual", "--output", "json")
    try:
        execution = json.loads(output or "null")
    except ValueError:
        execution = None
    name = (execution or {}).get("name")
    console.done(f"Started {console.value(app['name'])}: execution {console.value(name)}."
                 if name else f"Started {console.value(app['name'])}.")
    if not follow:
        return 0
    return logs(app, settings, ["--follow"], new=lambda run: run.id == name)


def logs(app: dict, settings: dict, rest: list[str],
         new: Callable[[runs_cli.Run], bool] | None = None) -> int:
    job, _current = find_job(settings, app["name"])
    return runs_cli.logs(lambda: list_runs(settings, job),
                         lambda run, since: read_lines(settings, job, run.id, since),
                         app["name"], rest,
                         store="Azure Log Analytics", delay=runs_cli.LOG_DELAY,
                         # Measured in October 2026: Log Analytics showed a line up to 133 s
                         # after the write. Only a run with no live log reads it.
                         hold=datetime.timedelta(seconds=150),
                         read_many=lambda chosen: read_many(settings, job,
                                                            [run.id for run in chosen]),
                         stream=lambda run, follow: stream_lines(settings, job, run, follow),
                         new=new)


def cost_estimate_for(region: str, currency: str, cron: str, job: str, rg: str,
               job_exists: bool, num_secrets: int,
               usage: tuple[int, int] | None) -> CostEstimate:
    console.status("Fetching list prices from the Azure Retail Prices API...")
    try:
        runs = config.runs_per_month(cron)
        seconds, basis = run_basis(average_run_seconds(job, rg) if job_exists else None)
        shown, converted = currency, ""
        try:
            items = priced_items(region, runs, seconds, basis, num_secrets, usage, currency)
            if currency != "USD":
                converted = f", in {currency} from the Azure Retail Prices API"
        except Exception:
            if currency == "USD":
                raise
            items = priced_items(region, runs, seconds, basis, num_secrets, usage, "USD")
            items, shown, converted = convert_from_usd(
                items, currency, ("Azure's rate", lambda: exchange_rate(currency)))
    except Exception as exc:
        fail(f"could not calculate the required monthly cost estimate: "
             f"{console.escape(str(exc))}")
    return cost_estimate(
        region, items, "excludes ACR storage and Log Analytics ingestion", shown,
        converted)


def priced_items(region: str, runs: float, seconds: float, basis: str, num_secrets: int,
                 usage: tuple[int, int] | None, currency: str) -> list[tuple[str, float]]:
    cpu_price, _ = retail_price(region, "Azure Container Apps",
                                "Standard vCPU Active Usage", "Standard", currency=currency)
    mem_price, _ = retail_price(region, "Azure Container Apps",
                                "Standard Memory Active Usage", "Standard", currency=currency)
    gib = float(MEMORY.rstrip("Gi"))
    run_cost = runs * seconds * (float(CPU) * cpu_price + gib * mem_price)
    acr_price, _ = retail_price(region, "Container Registry",
                                "Basic Registry Unit", "Basic", currency=currency)
    items = [
        (f"Container Apps job: ~{runs:.0f} runs x {basis} "
         f"x {float(CPU):g} vCPU / {gib:g} GiB", run_cost),
        ("Container Registry (Basic, shared)", acr_price * 30.44),
    ]
    if num_secrets:
        items.append(key_vault_item(region, runs, currency))
    if usage is not None:
        items.append(store_cost(usage, region, currency))
    return items


def environment_resource(settings: dict) -> dict | None:
    environment = settings["environment"]
    return az_json("containerapp", "env", "show", "--name", environment.name,
                   "--resource-group", environment.resource_group)


def check_environment(settings: dict, resource: dict | None) -> None:
    environment = settings["environment"]
    if environment.managed:
        require_managed(resource, f"Container Apps environment {console.value(environment)}")
        return
    if resource is None:
        fail(f"the Container Apps environment {console.value(environment)} named by "
             f"platform.environment in {console.value(config.PROJECT_FILE)} does not exist in "
             f"subscription {console.value(settings['subscription'])}")
    location = str(resource.get("location") or "").lower().replace(" ", "")
    if location and location != settings["region"]:
        fail(f"the Container Apps environment {console.value(environment)} is in "
             f"{console.value(location)}, but platform.region in "
             f"{console.value(config.PROJECT_FILE)} is {console.value(settings['region'])}. A "
             "job must run in its environment's region; change one of them")


def environment_actions(settings: dict, exists: bool, logs_exist: bool,
                        shared_group_exists: bool) -> list[console.Markup]:
    environment = settings["environment"]
    if not environment.managed:
        return [console.Markup(
            f"use your own Container Apps environment {console.value(environment)}")]
    shared = "(shared by every pdt project in this subscription)"
    return [
        console.Markup(("use existing" if shared_group_exists else "create")
                       + f" resource group {console.value(environment.resource_group)} {shared}"),
        console.Markup(("use existing" if logs_exist else "create")
                       + f" Log Analytics workspace {console.value(settings['workspace'])} in "
                       f"{console.value(environment.resource_group)}"),
        console.Markup(("use existing" if exists else "create")
                       + f" Container Apps environment {console.value(environment)} {shared}"),
    ]


@dataclasses.dataclass(frozen=True)
class Lock:
    """One app's CanNotDelete lock on a shared resource."""

    name: str
    label: str
    scope: tuple[str, ...]

    def exists(self) -> bool:
        return az_json("lock", "show", "--name", self.name, *self.scope) is not None

    def create(self) -> None:
        console.step(f"creating lock {console.value(self.name)} on {self.label}")
        run_quiet("lock", "create", "--name", self.name, "--lock-type", "CanNotDelete",
                  *self.scope, "--notes", "managed-by=pdt")

    def remove(self) -> None:
        console.step(f"removing lock {console.value(self.name)} from {self.label}")
        run_quiet("lock", "delete", "--name", self.name, *self.scope)


def registry_lock(settings: dict, owner: str) -> Lock:
    return Lock(owner, f"ACR {console.value(settings['registry'])}", (
        "--resource-group", settings["resource_group"],
        "--resource-name", settings["registry"],
        "--resource-type", "Microsoft.ContainerRegistry/registries"))


def environment_lock(settings: dict, owner: str) -> Lock:
    environment = settings["environment"]
    return Lock(clean_name(f"{owner}-in-{settings['resource_group']}"),
                f"Container Apps environment {console.value(environment)}", (
                    "--resource-group", environment.resource_group,
                    "--resource-name", environment.name,
                    "--resource-type", ENVIRONMENT_TYPE))


def lock_owner(app_name: str) -> str:
    """The name an app's locks carry, `pdt-<app>`, so renaming its job leaves them in place."""
    return clean_name(f"pdt-{app_name}")


def shared_locks(settings: dict, owner: str) -> list[Lock]:
    """The locks one app holds while deployed. A user's own environment gets none."""
    locks = [registry_lock(settings, owner)]
    if settings["environment"].managed:
        locks.append(environment_lock(settings, owner))
    return locks


def ensure_environment(settings: dict, exists: bool, logs_exist: bool,
                       shared_group: dict | None) -> None:
    environment = settings["environment"]
    if not environment.managed:
        return
    ensure_group(settings, environment.resource_group, shared_group)
    ensure_workspace(settings, logs_exist)
    if exists:
        return
    logs_id, logs_key = workspace_keys(settings)
    console.step(f"creating Container Apps environment {console.value(environment)}")
    run_quiet("containerapp", "env", "create", "--name", environment.name,
              "--resource-group", environment.resource_group,
              "--location", settings["region"],
              "--logs-workspace-id", logs_id, "--logs-workspace-key", logs_key,
              "--tags", "managed-by=pdt",
              hints={"EnvironmentsInSubExceeded": QUOTA_HINT})


def job_name(settings: dict[str, str], app_name: str) -> str:
    """pdt-<app>-<hash>, within Azure's 32 characters.

    Job names are unique across the shared environment, so the hash covers the
    project suffix as well as the app. A long app name loses its vowels first,
    then whole words from its start, then its end.
    """
    digest = hashlib.sha256(f"{app_name}/{settings['suffix']}".encode()).hexdigest()[:7]
    room = 32 - len(f"pdt--{digest}")
    words = re.sub(r"[^a-z0-9]+", "-", app_name.lower()).strip("-").split("-")
    if len("-".join(words)) > room:
        words = [word[:1] + re.sub(r"[aeiou]", "", word[1:]) for word in words]
    while len(words) > 1 and len("-".join(words)) > room:
        words.pop(0)
    part = "-".join(words)[:room]
    return f"pdt-{part}-{digest}" if part else f"pdt-{digest}"


def legacy_job_name(app_name: str) -> str:
    return clean_name(f"pdt-{app_name}")


def find_job(settings: dict[str, str], app_name: str) -> tuple[str, dict | None]:
    """The app's job under its current name, else under the name pdt used before `job_name`."""
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
    console.step(f"deleting Container Apps Job {console.value(job)}")
    run_quiet("containerapp", "job", "delete", "--name", job,
              "--resource-group", settings["resource_group"], "--yes")


def secrets(app: dict, action: str, assume_yes: bool, name: str | None = None) -> int:
    console.status("Checking your Azure sign-in...")
    settings = preflight(app, azure_settings(app))
    app_name = app["name"]
    job, _current = find_job(settings, app_name)
    sid = secret_name(app_name)
    _vault_exists, current = secret_state(settings, sid, app_name, True)

    def write(values: dict[str, str]) -> None:
        payload = json.dumps(values, sort_keys=True)
        secret_uri = ensure_secret(settings, sid, values, payload, current, app_name)
        identity_id = az_tsv("identity", "show", "--name", settings["identity"],
                             "--resource-group", settings["resource_group"], "--query", "id")
        set_job_secret(job, settings["resource_group"], secret_uri, identity_id)
        disable_old_secret_versions(settings, sid)

    return run_secrets(action, app, current, write, assume_yes, name)


def deploy(app: dict, assume_yes: bool) -> int:
    cloud = build_in_cloud(app, "ACR Tasks", "linux/amd64")
    console.status("Checking your Azure sign-in...")
    settings = preflight(app, azure_settings(app))
    name = app["name"]
    job = job_name(settings, name)
    cron = config.cron_expression(app["schedule"])
    values = gather_secrets(app)
    payload = json.dumps(values, sort_keys=True)
    sid = secret_name(name)
    rg = settings["resource_group"]

    console.status("Checking current state in Azure subscription "
                   f"{console.value(settings['subscription'])} "
                   f"({console.value(settings['region'])})...")
    check_shared_names(settings)
    group = az_json("group", "show", "--name", rg)
    require_managed(group, f"resource group {console.value(rg)}")
    group_exists = group is not None
    registry = az_json("acr", "show", "--name", settings["registry"],
                       "--resource-group", rg)
    require_managed(registry, f"ACR {console.value(settings['registry'])}")
    registry_exists = registry is not None
    environment = environment_resource(settings)
    check_environment(settings, environment)
    environment_exists = environment is not None
    identity = az_json("identity", "show", "--name", settings["identity"],
                       "--resource-group", rg)
    require_managed(identity, f"managed identity {console.value(settings['identity'])}")
    logs_exist = shared_group_exists = False
    shared_group = None
    if settings["environment"].managed:
        shared_group = az_json("group", "show", "--name", settings["environment"].resource_group)
        require_managed(shared_group,
                        f"resource group {console.value(settings['environment'].resource_group)}")
        shared_group_exists = shared_group is not None
        workspace = workspace_resource(settings)
        require_managed(workspace,
                        f"Log Analytics workspace {console.value(settings['workspace'])}")
        logs_exist = workspace is not None
    arm_auth_enabled = acr_arm_auth_enabled(registry)
    # The resource manager API, unlike `az containerapp job show`, reports runningState.
    current_job = az_json("rest", "--method", "get", "--url", job_url(settings, job))
    if current_job and not owned_by(current_job, name):
        fail(f"Container Apps Job {console.value(job)} already exists but is not owned by "
             f"PDT app {console.value(name)}; choose another resource group")
    locks = shared_locks(settings, lock_owner(name))
    missing_locks = [lock for lock in locks if not lock.exists()]
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

    image = f"{settings['registry']}.azurecr.io/{name}:latest"
    actions = ["register required Azure resource providers"]
    actions += environment_actions(settings, environment_exists, logs_exist,
                                   shared_group_exists)
    actions.append(console.Markup(("use existing" if group_exists else "create")
                                  + f" resource group {console.value(rg)}"))
    actions.append(console.Markup(("use existing" if registry_exists else "create")
                                  + f" ACR {console.value(settings['registry'])} (Basic)"))
    actions.append(console.Markup(
        ("keep" if arm_auth_enabled else "enable")
        + f" ACR authentication-as-arm on {console.value(settings['registry'])} "
        "(required for managed-identity image pulls)"))
    for lock in locks:
        actions.append(console.Markup(
            f"create lock {console.value(lock.name)} on {lock.label} (keeps it while "
            f"{console.value(name)} is deployed)"
            if lock in missing_locks
            else f"use existing lock {console.value(lock.name)} on {lock.label}"))
    actions.append(console.Markup(("use existing" if identity else "create")
                                  + f" managed identity {console.value(settings['identity'])}"))
    actions.append(console.Markup(("use existing" if vault_exists else "create")
                                  + f" Key Vault {console.value(settings['vault'])} (RBAC)"))
    actions.append("ensure scoped Key Vault secret permissions for the deployer "
                   "and managed identity")
    if store:
        actions += store_plan(store, store_present, name, job)
    actions.append(image_action(
        app, console.Markup(f"build and push image {console.value(image)}"),
        "ACR Tasks" if cloud else ""))
    actions += secret_actions(sid, values, current_secret, payload)
    if legacy_job:
        actions.append(console.Markup(f"delete Container Apps Job {console.value(legacy)} "
                                      f"(its name is now {console.value(job)})"))
    if values:
        actions.append(console.Markup(f"allow {console.value(job)} to update its own Key Vault "
                                      f"secret {console.value(sid)}"))
    actions.append(console.Markup(("update, if it changed," if current_job else "create")
                                  + f' Container Apps Job {console.value(job)}: '
                                  f'"{console.value(cron)}" (UTC)'
                                  + (" (paused)" if app["pause"] else "")))
    if not confirm(actions, assume_yes, cost_estimate_for(
            settings["region"], regions.local_currency(), cron, job, rg,
            current_job is not None,
            1 if values else 0, usage)):
        console.warn("Aborted; nothing was changed.")
        return DECLINED

    found = {"Microsoft.App": environment_exists or current_job is not None,
             "Microsoft.ContainerRegistry": registry_exists,
             "Microsoft.OperationalInsights": logs_exist,
             "Microsoft.KeyVault": vault_exists,
             "Microsoft.ManagedIdentity": identity is not None}
    ensure_environment(settings, environment_exists, logs_exist, shared_group)
    vault_id = ensure_group_and_vault(
        settings, tuple(name for name in (*COMMON_PROVIDERS, *PROVIDERS) if not found[name]),
        vault_exists, group)
    if not registry_exists:
        console.step(f"creating ACR {console.value(settings['registry'])}")
        run_quiet("acr", "create", "--name", settings["registry"],
                  "--resource-group", rg, "--location", settings["region"],
                  "--sku", "Basic", "--admin-enabled", "false",
                  "--tags", "managed-by=pdt")
    for lock in missing_locks:
        lock.create()
    if not identity:
        console.step(f"creating managed identity {console.value(settings['identity'])}")
        identity = az_json("identity", "create", "--name", settings["identity"],
                           "--resource-group", rg, "--location", settings["region"],
                           "--tags", "managed-by=pdt")
    if not identity:
        fail(f"could not read managed identity {console.value(settings['identity'])}")
    identity_id = identity["id"]
    principal_id = identity["principalId"]

    acr_id = resource_id(settings, "Microsoft.ContainerRegistry",
                         "registries", settings["registry"])
    assign_role(acr_id, principal_id, "AcrPull")
    assign_role(vault_id, principal_id, "Key Vault Secrets User")
    if store:
        ensure_store(settings, store, store_present)
    if not arm_auth_enabled:
        console.step(f"enabling ACR authentication-as-arm on {console.value(settings['registry'])}")
        enable_acr_arm_auth(settings["registry"])

    console.step(f"building image {console.value(image)} with "
                 f"{console.value('ACR Tasks') if cloud else 'Docker on this computer'}")
    build_image(app, settings, name, cloud)
    pinned = pushed_image(settings, name)
    secret_uri = ensure_secret(settings, sid, values, payload, current_secret, name)
    if legacy_job:
        retire_job(settings, legacy, legacy_job, store, sid)
    console.step(f"reconciling Container Apps Job {console.value(job)}")
    try:
        resource = reconcile_job(settings, job, pinned, cron, identity_id, secret_uri,
                                 store_url(store, name) if store else None, current_job, name)
    except SystemExit:
        report_job_failure(settings, job)
        raise
    set_job_paused(settings, job, app["pause"], resource)
    principal = ((resource or {}).get("identity") or {}).get("principalId") or job_principal_id(
        job, rg)
    if secret_uri:
        if current_secret != payload:
            disable_old_secret_versions(settings, sid)
        assign_role(secret_scope(settings, sid), principal, SECRET_ROLE)
    if store:
        assign_role(store["container_id"], principal, STORE_ROLE,
                    condition=store_condition(name))
    console.done(f"Deployed {console.value(name)}.")
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
        return Release(note=f"Container Apps environment {console.value(environment)} is your "
                            "own; pdt leaves it as it is")
    resource = environment_resource(settings)
    if resource is None:
        group = az_json("group", "show", "--name", environment.resource_group)
        return Release(group=managed_by_pdt(group) and not other_environments(settings))
    if not managed_by_pdt(resource):
        return Release()
    users = environment_users(settings)
    if users:
        return Release(note=users_note(settings, users))
    return Release(environment=True, group=not other_environments(settings))


def users_note(settings: dict, users: dict[str, int]) -> str:
    count = sum(users.values())
    return (f"Container Apps environment {console.value(settings['environment'])} still runs "
            f"{count} job{'s' if count != 1 else ''} in resource group"
            f"{'s' if len(users) != 1 else ''} "
            f"{', '.join(console.value(group) for group in sorted(users))}; keeping it")


def release_actions(settings: dict, release: Release) -> list[console.Markup]:
    environment = settings["environment"]
    actions = []
    if release.environment:
        actions.append(console.Markup(f"delete Container Apps environment "
                                      f"{console.value(environment)} (no other job uses it)"))
    if release.group:
        actions.append(console.Markup(
            f"delete resource group {console.value(environment.resource_group)} and the Log "
            f"Analytics workspace {console.value(settings['workspace'])} in it"))
    return actions


def release_environment(settings: dict, release: Release) -> None:
    environment = settings["environment"]
    if release.environment:
        users = environment_users(settings)
        if users:
            console.note(users_note(settings, users))
            return
        console.step(f"deleting Container Apps environment {console.value(environment)}")
        locked = delete_unless_locked(
            "containerapp", "env", "delete", "--name", environment.name,
            "--resource-group", environment.resource_group, "--yes")
        if locked:
            console.note(f"kept: Container Apps environment {console.value(environment)} "
                         f"(locked by {console.value(locked)})")
            return
    if release.group:
        console.step(f"deleting resource group {console.value(environment.resource_group)}")
        locked = delete_unless_locked("group", "delete", "--name", environment.resource_group, "--yes")
        if locked:
            console.note(f"kept: resource group {console.value(environment.resource_group)} "
                         f"(locked by {console.value(locked)})")


def kept_line(store: dict[str, str], deployer, name: str) -> console.Markup:
    usage = store_usage(deployer)
    if usage is None:
        return console.Markup(f"kept: {store_description(store)} "
                              f"(data under {console.value(f'{name}/')})")
    return store_kept_line(store_description(store), usage[0], name)


def destroy(app: dict, assume_yes: bool) -> int:
    console.status("Checking your Azure sign-in...")
    settings = preflight(app, azure_settings(app))
    name = app["name"]
    job, current_job = find_job(settings, name)
    sid = secret_name(name)
    rg = settings["resource_group"]
    managed_job = owned_by(current_job, name)
    secret_owned = managed_secret(settings, sid, name)
    if current_job and not managed_job:
        console.note(f"Container Apps Job {console.value(job)} is not owned by this app; "
                     "keeping it")
    store = store_settings(settings) if app["storage"] else None
    store_present = store_exists(store) if store else False
    deployer = deployer_store(settings, name) if store_present else None
    principal_id = ((current_job or {}).get("identity") or {}).get("principalId")
    grant = ""
    if deployer and managed_job and principal_id:
        grant = console.Markup(f"revoke {console.value(job)}'s write access to "
                               f"{console.value(f'{name}/')} in {store_description(store)}")
        warn_if_locked(deployer, name)
    others = other_pdt_apps(rg, name)
    # A run that stopped after the project group went may still owe the
    # environment, so a missing group takes the same path as a deletable one.
    group_exists = az_tsv("group", "exists", "--name", rg) == "true"
    held = [lock for lock in shared_locks(settings, lock_owner(name)) if lock.exists()]
    unlock = [console.Markup(f"remove lock {console.value(lock.name)} from {lock.label}")
              for lock in held]
    # A run stopped between the group delete and the purge leaves the vault
    # soft-deleted, still holding its global name.
    orphan_vault = not group_exists and managed_by_pdt((az_json(
        "keyvault", "show-deleted", "--name", settings["vault"]) or {}).get("properties"))
    if not group_exists or group_can_be_deleted(settings, others):
        release = environment_release(settings)
        actions = unlock[:]
        if grant:
            actions.append(grant)
        if group_exists:
            actions += [
                console.Markup(f"delete resource group {console.value(rg)} and everything in "
                               "it: the job and the shared ACR (with images), managed identity, "
                               "and Key Vault"),
                console.Markup(f"purge the soft-deleted Key Vault "
                               f"{console.value(settings['vault'])}"),
            ]
        if orphan_vault:
            actions.append(console.Markup(f"purge the soft-deleted Key Vault "
                                          f"{console.value(settings['vault'])}"))
        actions += release_actions(settings, release)
        if release.note:
            console.note(release.note)
        if not actions:
            console.done(f"Nothing owned by {console.value(name)} to remove.")
            return 0
        if not confirm(actions, assume_yes):
            console.warn("Aborted; nothing was changed.")
            return DECLINED
        for lock in held:
            lock.remove()
        if grant:
            revoke_role(store["container_id"], principal_id, STORE_ROLE)
        if orphan_vault:
            console.step(f"purging soft-deleted Key Vault {console.value(settings['vault'])}")
            run_quiet("keyvault", "purge", "--name", settings["vault"])
        if group_exists and not destroy_group(settings, name):
            # Another app joined the group meanwhile, so this one leaves the
            # way it would have with a sibling: its own job, image, and secret.
            remove_app(settings, name, job, sid, managed_job, secret_owned, principal_id,
                       has_image(settings, name))
            list_remaining(rg)
        else:
            release_environment(settings, release)
        if deployer:
            console.say(kept_line(store, deployer, name))
        return 0
    image_exists = has_image(settings, name)
    actions = unlock[:]
    if grant:
        actions.append(grant)
    if managed_job:
        actions.append(console.Markup(f"delete Container Apps Job {console.value(job)}"))
    if image_exists:
        actions.append(console.Markup(f"delete image repository {console.value(name)} from ACR "
                                      f"{console.value(settings['registry'])}"))
    if secret_owned:
        actions.append(console.Markup(f"delete and purge Key Vault secret {console.value(sid)}"))
    if not actions:
        console.done(f"Nothing owned by {console.value(name)} to remove in resource group "
                     f"{console.value(rg)}.")
        return 0
    if not confirm(actions, assume_yes):
        console.warn("Aborted; nothing was changed.")
        return DECLINED
    for lock in held:
        lock.remove()
    if grant:
        revoke_role(store["container_id"], principal_id, STORE_ROLE)
    remove_app(settings, name, job, sid, managed_job, secret_owned, principal_id, image_exists)
    report_shared_kept(rg, others)
    if deployer:
        console.say(kept_line(store, deployer, name))
    return 0


def has_image(settings: dict, name: str) -> bool:
    registry = az_json("acr", "show", "--name", settings["registry"],
                       "--resource-group", settings["resource_group"])
    registry_owned = managed_by_pdt(registry)
    if registry is not None and not registry_owned:
        console.note(f"ACR {console.value(settings['registry'])} is not managed by PDT; "
                     "keeping its images")
    return registry_owned and az_json(
        "acr", "repository", "show", "--name", settings["registry"],
        "--repository", name) is not None


def remove_app(settings: dict, name: str, job: str, sid: str, managed_job: bool,
               secret_owned: bool, principal_id: str | None, image_exists: bool) -> None:
    rg = settings["resource_group"]
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
