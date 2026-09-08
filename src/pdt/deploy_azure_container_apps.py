"""Deploy an app as a scheduled Azure Container Apps Job.

Entered through
deploy_azure.py, which owns the uv script header, login, and Key Vault.
The resource group, Container Apps environment, ACR, Key Vault, and
user-assigned identity are shared. Each app owns one tagged job.

The shared identity pulls the image and reads the app's Key Vault
secret. A job that uses the data store also gets its own system-assigned
identity, because the store grant carries a condition naming one app and
Azure keeps one assignment per principal, role, and scope.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import shutil
import subprocess

from pdt import config, console, terraform_azure
from pdt.deploy import confirm
from pdt.deploy_azure import (
    AZ, RECENT_RUNS, STORE_ROLE, assign_role, az_json, azure_settings,
    check_shared_names, clean_name, cost_estimate, deployer_store, ensure_secret,
    ensure_store, key_vault_item, managed_by_pdt, managed_secret,
    other_pdt_apps, owned_by, preflight, purge_secret, report_shared_kept, group_can_be_deleted,
    resource_id, retail_price, revoke_role, run_basis, run_quiet, run_stream,
    secret_actions, secret_name, secret_state, store_condition, store_cost,
    store_description, store_exists, store_plan, store_settings, store_url,
    terraform_shared_imports,
)
from pdt.deploy_common import (
    CostEstimate, fail, gather_secrets, image_action, stage_build_context,
    store_kept_line, warn_if_locked, write_dockerfile)
from pdt.terraform import Deployment

CPU = "0.5"
MEMORY = "1.0Gi"


def build_image(app: dict, registry: str, image_name: str) -> None:
    stage = stage_build_context(app)
    try:
        write_dockerfile(stage, app)
        run_stream("acr", "build", "--registry", registry, "--image",
                   f"{image_name}:latest", str(stage))
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def image_reference(registry: str, image_name: str) -> str:
    manifests = az_json("acr", "manifest", "list-metadata", "--registry", registry,
                        "--name", image_name) or []
    digest = next((item.get("digest") for item in manifests
                   if "latest" in (item.get("tags") or [])), None)
    if not digest:
        fail(f"could not find the image digest for {image_name}:latest in ACR {registry}")
    return f"{registry}.azurecr.io/{image_name}@{digest}"


def job_history_url(settings: dict[str, str], job: str) -> str:
    # The job's portal page; its Execution history tab lists every run, and
    # each run's Console link opens that run's logs. No deeper deep link exists.
    return ("https://portal.azure.com/#resource"
            + resource_id(settings, "Microsoft.App", "jobs", job))


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

    check_shared_names(settings)
    current_job = az_json("containerapp", "job", "show", "--name", job,
                          "--resource-group", rg)
    if current_job and not owned_by(current_job, name):
        fail(f"Container Apps Job {job} already exists but is not owned by "
             f"PDT app {name}; choose another resource group")
    vault_exists, current_hash = secret_state(settings, sid, name, values)
    store = store_settings(settings) if app["storage"] else None
    store_present = store_exists(store) if store else False
    deployer = deployer_store(settings, name) if store_present else None
    usage = (deployer.usage() if deployer else (0, 0)) if store else None
    image = f"{settings['registry']}.azurecr.io/{name}:latest"
    definition = {"name": name, "job": job, "cron": cron, "image": image}
    identity = {"subscription": settings["subscription"], "region": settings["region"],
                "resource_group": rg, "runtime": "container_apps"}
    actions = ["prepare protected Terraform state storage and deployment locking in Azure",
               "reconcile the shared Azure resources with Terraform",
               image_action(app, f"build and push image {image}"),
               *secret_actions(sid, values, current_hash, digest),
               ("update" if current_job else "create") + f" Container Apps Job {job} with Terraform"]
    if store:
        actions += store_plan(store, store_present, name, job)
    if not confirm(actions, assume_yes, cost_estimate_for(
            settings["region"], cron, job, rg, current_job is not None,
            1 if values else 0, usage)):
        console.warn("Aborted; nothing was changed.")
        return 1
    storage_url = store_url(store, name) if store else None
    with Deployment(app, "azure", identity, dict(os.environ), assume_yes) as deployment:
        deployment.save()
        shared = deployment.workspace("shared", terraform_azure.shared_configuration(settings, "container_apps"),
                                      terraform_shared_imports(settings, "container_apps"), retain=True)
        workspace = deployment.workspace("app", terraform_azure.container_app_configuration(definition, settings, None, storage_url),
                                         terraform_azure.container_app_imports(definition, settings) if current_job else {})
        shared.apply(shared.plan())
        if store:
            # The data store lives in its own resource group, so it outlives destroy
            # and stays outside the app's Terraform state.
            ensure_store(settings, store, store_present)
        console.step(f"building image {image}")
        build_image(app, settings["registry"], name)
        definition["image"] = image_reference(settings["registry"], name)
        secret_uri = ensure_secret(settings, sid, values, payload, digest, current_hash, name)
        workspace.reconcile(terraform_azure.container_app_configuration(definition, settings, secret_uri, storage_url))
        if store:
            assign_role(store["container_id"], job_principal_id(job, rg), STORE_ROLE,
                        condition=store_condition(name))
    console.done(f"Deployed {name}.")
    console.field("Run it once", f"pdt az containerapp job start --name {job} --resource-group {rg}")
    console.field("Run logs (Execution history tab)", job_history_url(settings, job))
    return 0


def destroy(app: dict, assume_yes: bool) -> int:
    settings = preflight(app, azure_settings(app))
    name = app["name"]
    job = clean_name(f"pdt-{name}")
    sid = secret_name(name)
    rg = settings["resource_group"]
    current_job = az_json("containerapp", "job", "show", "--name", job,
                          "--resource-group", rg)
    secret_owned = managed_secret(settings, sid, name)
    if current_job and not owned_by(current_job, name):
        fail(f"Container Apps Job {job} is not owned by this app; PDT will not remove it")
    definition = {"name": name, "job": job, "cron": config.cron_expression(app["schedule"]),
                  "image": f"{settings['registry']}.azurecr.io/{name}:latest"}
    identity = {"subscription": settings["subscription"], "region": settings["region"],
                "resource_group": rg, "runtime": "container_apps"}
    store = store_settings(settings) if app["storage"] else None
    store_present = store_exists(store) if store else False
    deployer = deployer_store(settings, name) if store_present else None
    principal_id = ((current_job or {}).get("identity") or {}).get("principalId")
    grant = ""
    if deployer and current_job and principal_id:
        grant = (f"revoke {job}'s write access to {name}/ "
                 f"in {store_description(store)}")
        warn_if_locked(deployer, name)
    actions = ["prepare protected Terraform state storage and deployment locking in Azure"]
    if grant:
        actions.append(grant)
    if current_job and owned_by(current_job, name):
        actions.append(f"delete Container Apps Job {job} and its Terraform-managed resources")
    if secret_owned:
        actions.append(f"delete and purge Key Vault secret {sid}")
    actions.append("delete shared Azure resources only when PDT owns the whole resource group")
    if not confirm(actions, assume_yes):
        console.warn("Aborted; nothing was changed.")
        return 1
    if grant:
        revoke_role(store["container_id"], principal_id, STORE_ROLE)
    with Deployment(app, "azure", identity, dict(os.environ), assume_yes) as deployment:
        workspace = deployment.existing_workspace("app")
        if workspace is None and current_job and owned_by(current_job, name):
            workspace = deployment.workspace("app", terraform_azure.container_app_configuration(definition, settings, None),
                                             terraform_azure.container_app_imports(definition, settings))
        plan = workspace.plan(destroy=True) if workspace else None
        registry = az_json("acr", "show", "--name", settings["registry"], "--resource-group", rg)
        image_exists = managed_by_pdt(registry) and az_json(
            "acr", "repository", "show", "--name", settings["registry"], "--repository", name) is not None
        if plan:
            workspace.apply(plan)
        if image_exists:
            run_stream("acr", "repository", "delete", "--name", settings["registry"],
                       "--repository", name, "--yes")
        if secret_owned:
            purge_secret(settings, sid)
        others = other_pdt_apps(rg, name)
        shared = deployment.existing_workspace("shared")
        if shared is None and group_can_be_deleted(settings, others):
            shared = deployment.workspace("shared", terraform_azure.shared_configuration(settings, "container_apps"),
                                          terraform_shared_imports(settings, "container_apps"), retain=True)
        if shared and group_can_be_deleted(settings, others):
            shared.remove_resources([f"{kind}.{instance}" for kind, instances in shared.configuration["resource"].items()
                                     for instance in instances])
        deployment.finish_destroy()
    report_shared_kept(rg, other_pdt_apps(rg, name))
    if deployer:
        console.say(store_kept_line(store_description(store), deployer.usage()[0], name))
    return 0
