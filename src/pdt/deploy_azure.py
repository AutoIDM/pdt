#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "azure-cli==2.89.1",
#     "azure-identity==1.25.3",
#     "azure-storage-blob",
#     "adlfs",
#     "fsspec",
#     "duckdb==1.5.5",
#     "pyyaml",
#     "rich",
#     "python-dotenv",
#     "backoff",
# ]
#
# [tool.uv]
# prerelease = "allow"
# ///
"""Deploy an app to Azure.

Shared login, subscription, resource group, Key Vault, price, and cost code
lives here. The job itself, a scheduled Container Apps Job, is in
deploy_azure_container_apps.py.

The Azure CLI is a Python package, so the script header installs it and
every call here runs it as `python -m azure.cli`. No system install is
needed. Login state lives in ~/.azure either way. azure-cli pins a few of
its own dependencies to pre-release versions, so the header allows
pre-releases; without that, uv before 0.12 refuses to resolve it.

Every app owns one tagged Key Vault secret holding its env vars as one
json blob, exposed to the job as PDT_ENV_JSON. Secret values are sent to
Azure through a protected temporary file, never on the command line.

The data store is storage account pdtdata<suffix> in its own resource
group pdt-data, so it survives destroy of resource group pdt. Blob
containers carry no ARM tags, so the container holds STORE_TAGS as
metadata instead. RBAC scope stops at the container, so store_condition
adds an attribute-based access control condition that holds the job
inside its own <app>/ folder.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pdt import config, console, storage_cli
from pdt.deploy_common import (
    STORE_TAGS, CostEstimate, fail, fetch_json, store_cost_label, store_plan_lines,
    store_suffix)
from pdt.utils.email_auth import can_prompt
from pdt.utils.storage import Store

AZ = [sys.executable, "-m", "azure.cli"]
COMMON_PROVIDERS = ("Microsoft.KeyVault", "Microsoft.ManagedIdentity")
PLACEHOLDER_SUBSCRIPTION = "00000000-0000-0000-0000-000000000000"
PRICES_API = "https://prices.azure.com/api/retail/prices"
ASSUMED_RUN_MINUTES = 5.0
RECENT_RUNS = 3
STORE_GROUP = "pdt-data"
STORE_ROLE = "Storage Blob Data Contributor"
STORE_TAG_ARGS = tuple(f"{key}={value}" for key, value in STORE_TAGS.items())
BLOB_ACTION = "Microsoft.Storage/storageAccounts/blobServices/containers/blobs"


def run_quiet(*args: str, data: str | None = None, retry_access: bool = False) -> str:
    waits = (10, 20, 40, 0) if retry_access else (0,)
    for wait in waits:
        proc = subprocess.run(
            [*AZ, *args], input=data, capture_output=True, text=True)
        if proc.returncode == 0:
            return proc.stdout
        transient = any(text in proc.stderr.lower() for text in (
            "unable to fetch secret", "forbidden", "authorizationfailed",
            "does not have authorization",
        ))
        if wait == 0 or not transient:
            break
        console.bullet(f"Azure RBAC is still propagating; retrying in {wait}s...", indent=4)
        time.sleep(wait)
    console.say(proc.stderr.strip())
    fail(f"pdt az {' '.join(args[:4])} failed; fix the problem above and re-run")


def run_stream(*args: str) -> None:
    proc = subprocess.run([*AZ, *args])
    if proc.returncode != 0:
        fail(f"pdt az {' '.join(args[:3])} failed; fix the problem above and re-run")


def az_json(*args: str):
    proc = subprocess.run(
        [*AZ, *args, "--output", "json"], stdin=subprocess.DEVNULL,
        capture_output=True, text=True)
    if proc.returncode != 0:
        return None
    return json.loads(proc.stdout or "null")


def az_tsv(*args: str) -> str:
    return run_quiet(*args, "--output", "tsv").strip()


def clean_name(value: str, limit: int = 32) -> str:
    name = re.sub(r"[^a-z0-9-]+", "-", value.lower()).strip("-")
    if len(name) <= limit:
        return name
    suffix = hashlib.sha256(name.encode()).hexdigest()[:7]
    return f"{name[:limit - 8].rstrip('-')}-{suffix}"


def azure_settings(app: dict) -> dict[str, str]:
    platform = app["platform"]
    subscription = str(
        platform.get("subscription")
        or os.environ.get("PDT_AZURE_SUBSCRIPTION") or "")
    region = str(
        platform.get("region") or os.environ.get("PDT_AZURE_REGION")
        or "eastus")
    resource_group = str(
        platform.get("resource_group")
        or os.environ.get("PDT_AZURE_RESOURCE_GROUP") or "pdt")
    return {
        "subscription": subscription,
        "region": region,
        "resource_group": resource_group,
        "environment": str(
            os.environ.get("PDT_AZURE_CONTAINER_APPS_ENVIRONMENT")
            or "pdt"),
        "identity": str(
            os.environ.get("PDT_AZURE_MANAGED_IDENTITY")
            or "pdt-runner"),
        # Log Analytics workspace names must be 4 to 63 characters, so this
        # default cannot be the bare "pdt" the other shared names start from.
        "workspace": str(
            os.environ.get("PDT_AZURE_LOG_WORKSPACE")
            or "pdt-logs"),
    }


def shared_names(subscription: str) -> dict[str, str]:
    # Storage account and Key Vault names are global across Azure, so they
    # carry a hash of the subscription.
    suffix = hashlib.sha256(subscription.encode()).hexdigest()[:10]
    return {
        "suffix": suffix,
        "registry": str(
            os.environ.get("PDT_AZURE_CONTAINER_REGISTRY")
            or f"pdt{suffix}")[:50].replace("-", ""),
        "storage": str(
            os.environ.get("PDT_AZURE_STORAGE_ACCOUNT")
            or f"pdt{suffix}")[:24].replace("-", ""),
        "vault": str(
            os.environ.get("PDT_AZURE_KEY_VAULT")
            or f"pdt-{suffix}")[:24].strip("-"),
    }


def preflight(app: dict, settings: dict[str, str]) -> dict[str, str]:
    requested = settings["subscription"]
    if requested == PLACEHOLDER_SUBSCRIPTION:
        requested = ""
    can_ask = can_prompt(None)
    account = az_json("account", "show")
    if not account:
        if not can_ask:
            fail("no Azure sign-in on this computer; run `az login "
                 "--service-principal -u <id> -p <secret> --tenant <tenant>` "
                 "before this command")
        console.warn("You are not logged in to Azure yet.")
        try:
            answer = input("Log in now (opens a browser)? [y/N] ").strip().lower()
        except EOFError:
            answer = ""
        if answer not in ("y", "yes"):
            fail("Azure login is required; run the same command again and answer y")
        login(requested)
        account = az_json("account", "show")
        if not account:
            fail("Azure login failed")
    if requested and requested not in (account.get("id"), account.get("name")):
        account = choose_subscription(app, requested, can_ask)
        run_quiet("account", "set", "--subscription", account["id"])
    elif not requested and can_ask:
        save_subscription(app, account)
    settings["subscription"] = str(account["id"])
    settings.update(shared_names(settings["subscription"]))
    user = account.get("user") or {}
    is_user = str(user.get("type", "")).lower() == "user"
    deployer_id = os.environ.get("PDT_AZURE_DEPLOYER_OBJECT_ID", "").strip()
    if not deployer_id:
        deployer_id = object_id_from_token(access_token())
    if not deployer_id:
        fail("cannot determine the signed-in Azure principal; set "
             "PDT_AZURE_DEPLOYER_OBJECT_ID")
    settings["deployer_object_id"] = deployer_id
    settings["deployer_principal_type"] = "User" if is_user else "ServicePrincipal"
    return settings


def object_id_from_token(token: str) -> str:
    parts = token.split(".")
    if len(parts) != 3:
        return ""
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except ValueError:
        return ""
    return str(claims.get("oid") or "")


def access_token() -> str:
    return az_tsv("account", "get-access-token", "--query", "accessToken")


def save_subscription(app: dict, sub: dict) -> None:
    saved = config.save_platform_key(app, "subscription", sub["id"])
    console.done(f"Saved subscription: {sub['id']} to {saved.relative_to(config.find_project())}.")


def choose_subscription(app: dict, requested: str, can_ask: bool) -> dict:
    available = az_json("account", "list", "--all") or []
    if not available:
        fail("your Azure account has no subscription yet; create one at "
             "https://portal.azure.com/#view/Microsoft_Azure_Billing/SubscriptionsBladeV2")
    for sub in available:
        if requested in (sub.get("id"), sub.get("name")):
            return sub
    console.warn(f"platform.subscription {requested!r} in pdt.yml is not one of your subscriptions.")
    if not can_ask:
        fail("no Azure subscription selected; set platform.subscription in "
             "pdt.yml, or set PDT_AZURE_SUBSCRIPTION, to a subscription id "
             "this login can use")
    console.heading("Your Azure subscriptions:")
    for index, sub in enumerate(available, 1):
        console.choice(index, str(sub.get("name")), str(sub.get("id")))
    try:
        answer = input(f"Deploy to which one? [1-{len(available)}] ").strip()
    except EOFError:
        answer = ""
    if not answer.isdigit() or not 1 <= int(answer) <= len(available):
        fail("no Azure subscription selected")
    sub = available[int(answer) - 1]
    save_subscription(app, sub)
    return sub


def login(requested: str) -> None:
    console.status("Opening your browser for the Azure login...")
    proc = subprocess.run([*AZ, "login"], capture_output=True, text=True)
    if proc.returncode == 0:
        return
    output = proc.stdout + proc.stderr
    if "No subscriptions found" not in output:
        console.say(output.strip())
        fail("pdt az login failed; fix the problem above and re-run")
    user = re.search(r"No subscriptions found for (\S+)\.", output)
    who = user.group(1) if user else "your Azure account"
    console.warn(f"The login worked, but {who} has no Azure subscription.")
    console.say("Azure bills every resource to a subscription, so deploy cannot continue without one.")
    console.bullet("1. Create one at https://portal.azure.com/#view/Microsoft_Azure_Billing/SubscriptionsBladeV2")
    console.bullet("(an Azure free account also works: https://azure.microsoft.com/free).", indent=5)
    console.bullet("2. Put its Subscription ID in platform.subscription in pdt.yml.")
    console.bullet("3. Run the same command again.")
    if requested in output:
        console.note(f"{requested} in pdt.yml is your tenant (directory) id, not a subscription id.")
    raise SystemExit(1)


def relogin(requested: str) -> int:
    console.status("Clearing the cached Azure login on this computer...")
    subprocess.run([*AZ, "account", "clear"], stdin=subprocess.DEVNULL,
                   capture_output=True, text=True)
    console.say("Choose a different account in the browser to sign in as someone else.")
    login(requested)
    account = az_json("account", "show")
    if not account:
        fail("Azure login failed")
    console.done(f"Signed in as {(account.get('user') or {}).get('name') or 'unknown'}")
    console.field("Subscription", f"{account.get('name')} ({account.get('id')})")
    return 0


def resource_id(settings: dict[str, str], provider: str, kind: str, name: str) -> str:
    return (f"/subscriptions/{settings['subscription']}/resourceGroups/"
            f"{settings['resource_group']}/providers/{provider}/{kind}/{name}")


def secret_name(app_name: str) -> str:
    return clean_name(f"pdt-{app_name}-env", 127)


def set_key_vault_secret(vault: str, name: str, payload: str,
                         digest: str, app_name: str) -> str:
    fd, filename = tempfile.mkstemp(prefix="pdt-secret-")
    try:
        os.chmod(filename, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(payload)
        return run_quiet(
            "keyvault", "secret", "set", "--vault-name", vault, "--name", name,
            "--file", filename, "--encoding", "utf-8", "--tags",
            "managed-by=pdt", f"pdt-app={app_name}",
            f"pdt-hash={digest}", "--query", "id", "--output", "tsv",
            retry_access=True).strip()
    finally:
        Path(filename).unlink(missing_ok=True)


def assign_role(scope: str, principal_id: str, role: str,
                principal_type: str = "ServicePrincipal", condition: str = "") -> None:
    existing = az_json(
        "role", "assignment", "list", "--assignee", principal_id,
        "--role", role, "--scope", scope) or []
    if existing and (existing[0].get("condition") or "") == condition:
        return
    if existing:
        run_quiet("role", "assignment", "delete", "--assignee", principal_id,
                  "--role", role, "--scope", scope)
    args = ["role", "assignment", "create", "--assignee-object-id", principal_id,
            "--assignee-principal-type", principal_type, "--role", role,
            "--scope", scope]
    if condition:
        args += ["--condition", condition, "--condition-version", "2.0"]
    run_quiet(*args)


def revoke_role(scope: str, principal_id: str, role: str) -> None:
    existing = az_json(
        "role", "assignment", "list", "--assignee", principal_id,
        "--role", role, "--scope", scope)
    if not existing:
        return
    run_quiet("role", "assignment", "delete", "--assignee", principal_id,
              "--role", role, "--scope", scope)


def retail_price(region: str, service: str, meter: str, sku: str,
                 product: str = "") -> tuple[float, str]:
    query = (f"serviceName eq '{service}' and armRegionName eq '{region}' "
             f"and meterName eq '{meter}' and skuName eq '{sku}' "
             f"and type eq 'Consumption'")
    if product:
        query += f" and productName eq '{product}'"
    url = f"{PRICES_API}?$filter={urllib.parse.quote(query)}"
    items = fetch_json(url, timeout=30).get("Items") or []
    items = [i for i in items if i.get("retailPrice")]
    if not items:
        raise LookupError(f"no {meter!r} price for {service} in region {region}")
    return float(items[0]["retailPrice"]), items[0].get("unitOfMeasure", "")


def owned_by(resource: dict | None, app_name: str) -> bool:
    tags = (resource or {}).get("tags") or {}
    return (tags.get("managed-by"), tags.get("pdt-app")) == ("pdt", app_name)


def managed_by_pdt(resource: dict | None) -> bool:
    return ((resource or {}).get("tags") or {}).get("managed-by") == "pdt"


def require_managed(resource: dict | None, label: str) -> None:
    if resource is not None and not managed_by_pdt(resource):
        fail(f"{label} exists but is not managed by PDT")


SHARED_TYPES = {
    "microsoft.storage/storageaccounts": ("Storage account", "storage"),
    "microsoft.keyvault/vaults": ("Key Vault", "vault"),
    "microsoft.containerregistry/registries": ("Container Registry", "registry"),
}


def check_shared_names(settings: dict[str, str]) -> None:
    rg = settings["resource_group"]
    for resource in az_json("resource", "list", "--resource-group", rg) or []:
        shared = SHARED_TYPES.get(str(resource.get("type", "")).lower())
        if shared is None or not managed_by_pdt(resource):
            continue
        label, key = shared
        if resource.get("name") != settings[key]:
            fail(f"an earlier deploy created the {label} {resource['name']} in Azure. "
                 f"This deploy would create a second one, {settings[key]}, and leave "
                 f"the first one unused. This happens when the subscription or the "
                 f"resource group in {config.PROJECT_FILE} changed after that deploy.\n"
                 f"If other apps still use {resource['name']}, put the earlier "
                 f"subscription and resource group back in {config.PROJECT_FILE}.\n"
                 f"If nothing uses it, remove it and deploy again:\n"
                 f"  pdt az resource delete --ids {resource.get('id')}")


def secret_state(settings: dict[str, str], sid: str, app_name: str,
                 values: dict) -> tuple[bool, str | None]:
    vault = az_json("keyvault", "show", "--name", settings["vault"],
                    "--resource-group", settings["resource_group"])
    require_managed(vault, f"Key Vault {settings['vault']}")
    vault_exists = vault is not None
    current = None
    if vault_exists and values:
        current = az_json("keyvault", "secret", "show", "--vault-name",
                          settings["vault"], "--name", sid)
        if current and not owned_by(current, app_name):
            fail(f"Key Vault secret {sid} already exists but is not owned "
                 f"by PDT app {app_name}; choose another Key Vault")
    current_hash = (current.get("tags") or {}).get("pdt-hash") if current else None
    return vault_exists, current_hash


def secret_actions(sid: str, values: dict, current_hash: str | None,
                   digest: str) -> list[str]:
    if not values:
        return []
    state = "unchanged" if current_hash == digest else (
        "update" if current_hash else "create")
    return [f"{state} Key Vault secret {sid} ({len(values)} env vars)"]


def register_providers(names: tuple[str, ...]) -> None:
    pending = [name for name in names
               if az_tsv("provider", "show", "--namespace", name,
                         "--query", "registrationState") != "Registered"]
    if not pending:
        return
    console.step(f"registering Azure providers: {', '.join(pending)}")
    console.bullet("(a new subscription can take several minutes for this)", indent=4)
    for name in pending:
        run_quiet("provider", "register", "--namespace", name)
    waited = 0
    while pending:
        time.sleep(10)
        waited += 10
        pending = [name for name in pending
                   if az_tsv("provider", "show", "--namespace", name,
                             "--query", "registrationState") != "Registered"]
        if pending:
            console.bullet(f"still waiting after {waited}s for: {', '.join(pending)}", indent=4)


def ensure_group_and_vault(settings: dict[str, str], providers: tuple[str, ...],
                           vault_exists: bool) -> str:
    rg = settings["resource_group"]
    group = az_json("group", "show", "--name", rg)
    require_managed(group, f"resource group {rg}")
    register_providers((*COMMON_PROVIDERS, *providers))
    console.step(f"reconciling resource group {rg}")
    run_quiet("group", "create", "--name", rg, "--location", settings["region"],
              "--tags", "managed-by=pdt")
    if not vault_exists:
        console.step(f"creating Key Vault {settings['vault']}")
        run_quiet("keyvault", "create", "--name", settings["vault"],
                  "--resource-group", rg, "--location", settings["region"],
                  "--enable-rbac-authorization", "true",
                  "--tags", "managed-by=pdt")
    if not workspace_exists(settings):
        console.step(f"creating Log Analytics workspace {settings['workspace']}")
        run_quiet("monitor", "log-analytics", "workspace", "create",
                  "--resource-group", rg, "--workspace-name", settings["workspace"],
                  "--location", settings["region"], "--tags", "managed-by=pdt")
    vault_id = resource_id(settings, "Microsoft.KeyVault", "vaults", settings["vault"])
    assign_role(vault_id, settings["deployer_object_id"], "Key Vault Secrets Officer",
                settings["deployer_principal_type"])
    return vault_id


def workspace_resource(settings: dict[str, str]) -> dict | None:
    return az_json("monitor", "log-analytics", "workspace", "show",
                   "--resource-group", settings["resource_group"],
                   "--workspace-name", settings["workspace"])


def workspace_exists(settings: dict[str, str]) -> bool:
    return workspace_resource(settings) is not None


def ensure_secret(settings: dict[str, str], sid: str, values: dict,
                  payload: str, digest: str, current_hash: str | None,
                  app_name: str) -> str | None:
    if not values:
        return None
    if current_hash != digest:
        console.step(f"writing Key Vault secret {sid}")
        return set_key_vault_secret(settings["vault"], sid, payload, digest, app_name)
    return az_tsv("keyvault", "secret", "show", "--vault-name", settings["vault"],
                  "--name", sid, "--query", "id")


def managed_secret(settings: dict[str, str], sid: str, app_name: str) -> bool:
    secret = az_json("keyvault", "secret", "show", "--vault-name",
                     settings["vault"], "--name", sid)
    return owned_by(secret, app_name)


def delete_secret(settings: dict[str, str], sid: str) -> None:
    run_quiet("keyvault", "secret", "delete", "--vault-name",
              settings["vault"], "--name", sid, retry_access=True)


def other_pdt_apps(rg: str, exclude_app: str) -> list[str]:
    resources = az_json("resource", "list", "--resource-group", rg) or []
    names = set()
    for resource in resources:
        tags = resource.get("tags") or {}
        if tags.get("managed-by") == "pdt" and tags.get("pdt-app"):
            names.add(tags["pdt-app"])
    names.discard(exclude_app)
    return sorted(names)


PLAN_TYPE = "microsoft.web/serverfarms"
INSIGHTS_TYPE = "microsoft.insights/components"
SMART_RULE_TYPE = "microsoft.alertsmanagement/smartDetectorAlertRules"
ACTION_GROUP_TYPE = "microsoft.insights/actionGroups"
SMART_ACTION_GROUP = "Application Insights Smart Detection"


def failure_rule_name(function_app: str) -> str:
    return f"Failure Anomalies - {function_app}"


def side_resource(rg: str, name: str, kind: str) -> dict | None:
    return az_json("resource", "show", "--resource-group", rg,
                   "--name", name, "--resource-type", kind)


def tag_side_resource(rg: str, name: str, kind: str, *tags: str) -> None:
    # Azure creates these next to a Function App; tag them so destroy owns them.
    if side_resource(rg, name, kind):
        run_quiet("resource", "tag", "--resource-group", rg, "--name", name,
                  "--resource-type", kind, "--tags", *tags)


def platform_side_resource(resource: dict) -> bool:
    kind = str(resource.get("type", "")).lower()
    name = str(resource.get("name", ""))
    return (kind == PLAN_TYPE
            or (kind == SMART_RULE_TYPE.lower() and name.startswith("Failure Anomalies - "))
            or (kind == ACTION_GROUP_TYPE.lower() and name == SMART_ACTION_GROUP))


def group_can_be_deleted(settings: dict[str, str], others: list[str]) -> bool:
    if others:
        return False
    rg = settings["resource_group"]
    group = az_json("group", "show", "--name", rg)
    if not managed_by_pdt(group):
        return False
    resources = az_json("resource", "list", "--resource-group", rg)
    if resources is None:
        return False
    # Older pdt versions left the plans, alert rules, and action group that
    # Azure creates beside a Function App untagged; they go with the group.
    return all(managed_by_pdt(resource) or platform_side_resource(resource)
               for resource in resources)


def purge_secret(settings: dict[str, str], sid: str) -> None:
    # No recovery windows: purge right after the soft delete lands.
    delete_secret(settings, sid)
    for _ in range(30):
        if az_json("keyvault", "secret", "show-deleted", "--vault-name",
                   settings["vault"], "--name", sid):
            break
        time.sleep(2)
    run_quiet("keyvault", "secret", "purge", "--vault-name",
              settings["vault"], "--name", sid, retry_access=True)


def destroy_group(settings: dict[str, str]) -> None:
    rg = settings["resource_group"]
    console.step(f"deleting resource group {rg} (takes a few minutes)")
    run_quiet("group", "delete", "--name", rg, "--yes")
    if az_json("keyvault", "show-deleted", "--name", settings["vault"]):
        console.step(f"purging soft-deleted Key Vault {settings['vault']}")
        run_quiet("keyvault", "purge", "--name", settings["vault"])
    if az_tsv("group", "exists", "--name", rg) == "false":
        console.done(f"Nothing remains in resource group {rg}.")


def report_shared_kept(rg: str, others: list[str]) -> None:
    if others:
        console.note(f"apps still deployed in resource group {rg}: {', '.join(others)}. "
                     "Shared resources stay until the last app is destroyed.")
    else:
        console.note(f"resource group {rg} is not fully owned by PDT, so PDT kept it.")
    console.heading("Still present:")
    for resource in az_json("resource", "list", "--resource-group", rg) or []:
        console.bullet(f"{resource.get('name')}  ({resource.get('type')})")


def store_settings(settings: dict[str, str]) -> dict[str, str]:
    suffix = store_suffix(settings["subscription"])
    account = f"pdtdata{suffix}"
    container = f"pdt-data-{suffix}"
    return {
        "group": STORE_GROUP,
        "account": account,
        "container": container,
        "container_id": (
            f"/subscriptions/{settings['subscription']}/resourceGroups/{STORE_GROUP}"
            f"/providers/Microsoft.Storage/storageAccounts/{account}"
            f"/blobServices/default/containers/{container}"),
    }


def store_url(store: dict[str, str], app_name: str) -> str:
    return (f"abfs://{store['container']}@{store['account']}.dfs.core.windows.net"
            f"/{app_name}/")


def store_description(store: dict[str, str]) -> str:
    return f"storage account {store['account']}, container {store['container']}"


def store_exists(store: dict[str, str]) -> bool:
    group = az_json("group", "show", "--name", store["group"])
    require_managed(group, f"resource group {store['group']}")
    account = az_json("storage", "account", "show", "--name", store["account"],
                      "--resource-group", store["group"])
    require_managed(account, f"Storage account {store['account']}")
    if account is None:
        return False
    container = az_json("storage", "container-rm", "show",
                        "--storage-account", store["account"],
                        "--resource-group", store["group"], "--name", store["container"])
    if container is None:
        return False
    if (container.get("metadata") or {}).get("managed-by") != "pdt":
        fail(f"container {store['container']} exists but is not managed by PDT")
    return True


def store_condition(app_name: str) -> str:
    """Limit Storage Blob Data Contributor to the app's own folder.

    The grammar and the action names come from "Example Azure role assignment
    conditions for Blob Storage":
    https://learn.microsoft.com/en-us/azure/storage/blobs/storage-auth-abac-examples
    """
    read = f"ActionMatches{{'{BLOB_ACTION}/read'}}"
    return (
        f"((!({read} AND NOT SubOperationMatches{{'Blob.List'}})"
        f" AND !(ActionMatches{{'{BLOB_ACTION}/write'}})"
        f" AND !(ActionMatches{{'{BLOB_ACTION}/delete'}}))"
        f" OR (@Resource[{BLOB_ACTION}:path] StringStartsWith '{app_name}/'))"
        f" AND ((!({read} AND SubOperationMatches{{'Blob.List'}}))"
        f" OR (@Request[{BLOB_ACTION}:prefix] StringStartsWith '{app_name}/'))")


def store_plan(store: dict[str, str], exists: bool, app_name: str,
               identity: str) -> list[str]:
    return store_plan_lines(store_description(store), exists, identity, app_name) + [
        f"grant the signed-in Azure account write access to {store['container']} "
        "(for pdt storage)",
    ]


def ensure_store(settings: dict[str, str], store: dict[str, str], exists: bool) -> None:
    if not exists:
        register_providers(("Microsoft.Storage",))
        console.step(f"creating {store_description(store)}")
        run_quiet("group", "create", "--name", store["group"],
                  "--location", settings["region"], "--tags", *STORE_TAG_ARGS)
        run_quiet("storage", "account", "create", "--name", store["account"],
                  "--resource-group", store["group"], "--location", settings["region"],
                  "--sku", "Standard_LRS", "--allow-blob-public-access", "false",
                  "--min-tls-version", "TLS1_2", "--tags", *STORE_TAG_ARGS)
        run_quiet("storage", "container-rm", "create",
                  "--storage-account", store["account"],
                  "--resource-group", store["group"], "--name", store["container"],
                  "--metadata", *STORE_TAG_ARGS)
    assign_role(store["container_id"], settings["deployer_object_id"], STORE_ROLE,
                settings["deployer_principal_type"])


def store_cost(usage: tuple[int, int], region: str) -> tuple[str, float]:
    count, size = usage
    price, _ = retail_price(region, "Storage", "Hot LRS Data Stored", "Hot LRS",
                            "General Block Blob v2")
    return store_cost_label(count, size), size / 1024 ** 3 * price


def deployer_store(settings: dict[str, str], app_name: str) -> Store:
    from azure.identity import AzureCliCredential
    os.environ["PATH"] = os.pathsep.join(
        [str(Path(sys.executable).parent), os.environ.get("PATH", "")])
    return Store(store_url(store_settings(settings), app_name), AzureCliCredential())


def storage(app: dict, settings: dict[str, str], rest: list[str], assume_yes: bool) -> int:
    return storage_cli.run(deployer_store(settings, app["name"]), app, rest, assume_yes)


def run_basis(seconds: float | None) -> tuple[float, str]:
    if seconds is None:
        return ASSUMED_RUN_MINUTES * 60, f"{ASSUMED_RUN_MINUTES:g} min assumed"
    return seconds, f"{seconds / 60:.1f} min avg of recent runs"


def key_vault_item(region: str, runs: float) -> tuple[str, float]:
    kv_price, _ = retail_price(region, "Key Vault", "Operations", "Standard")
    return f"Key Vault: 1 secret, ~{runs:.0f} reads", runs * kv_price / 10000


def cost_estimate(region: str, items: list[tuple[str, float]],
                  excludes: str) -> CostEstimate:
    return CostEstimate(items, f"{region} list prices, before free grants", excludes)


def load_app(app_name: str) -> dict:
    try:
        app = config.merged_app(app_name)
    except config.ConfigError as exc:
        fail(str(exc))
    config.load_env(app["dir"])
    return app


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "az":
        return subprocess.run([*AZ, *sys.argv[2:]]).returncode
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("deploy", "destroy", "login", "storage"))
    parser.add_argument("app")
    parser.add_argument("rest", nargs="*")
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--profile", help="not used by Azure")
    args = parser.parse_intermixed_args()
    app = load_app(args.app)
    if args.command == "login":
        requested = azure_settings(app)["subscription"]
        return relogin("" if requested == PLACEHOLDER_SUBSCRIPTION else requested)
    if args.command == "storage":
        return storage(app, preflight(app, azure_settings(app)), args.rest, args.yes)
    if app["timezone"] not in ("Etc/UTC", "UTC"):
        fail("Azure evaluates cron schedules only in UTC; set timezone: Etc/UTC")
    from pdt import deploy_azure_container_apps as module
    if args.command == "deploy":
        return module.deploy(app, args.yes)
    return module.destroy(app, args.yes)


if __name__ == "__main__":
    sys.exit(main())
