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
`az` runs it inside this process. A new `python -m azure.cli` process for
each call cost 2 to 4 seconds on Windows, and a deploy makes dozens of
calls. Only the browser login and `pdt az` start a process, because
they stream to the terminal. No system install is
needed. Login state lives in ~/.azure either way. azure-cli pins a few of
its own dependencies to pre-release versions, so the header allows
pre-releases; without that, uv before 0.12 refuses to resolve it.

Every app owns one tagged Key Vault secret holding its env vars as one
json blob, exposed to the job as PDT_ENV_JSON. Secret values are sent to
Azure through a protected temporary file, never on the command line.

The data store is storage account pdtdata<suffix> in its own resource
group pdt-data, so it survives destroy of resource group pdt. Blob
containers carry no ARM tags, so the container holds STORE_TAGS as
metadata instead, with underscores in the keys because metadata keys
must be C# identifiers. RBAC scope stops at the container, so store_condition
adds an attribute-based access control condition that holds the job
inside its own <app>/ folder.

A shared resource (the project's ACR, and the Container Apps environment in
deploy_azure_container_apps.py) has a set of users, and destroy may delete
it only while that set is empty and nobody can join it. Each deployed app
holds a CanNotDelete management lock on the resource, so Azure refuses the
delete while any other app is deployed: the check and the delete are one
operation on Azure's side. `delete_unless_locked` turns that refusal into
"kept", not a failure. Right before each delete, destroy also re-reads the
users (`destroy_group`), which covers a deploy from an older pdt that takes
no lock.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pdt import config, console, storage_cli
from pdt.deploy_common import (
    STORE_TAGS, CostEstimate, fail, fetch_json, secret_contents, store_cost_label,
    store_plan_lines, store_suffix)
from pdt.utils.email_auth import can_prompt
from pdt.utils.storage import Store

AZ = [sys.executable, "-m", "azure.cli"]
COMMON_PROVIDERS = ("Microsoft.KeyVault", "Microsoft.ManagedIdentity")
PLACEHOLDER_SUBSCRIPTION = "00000000-0000-0000-0000-000000000000"
# A subscription allows a fixed number of Container Apps environments, so
# every pdt project in a subscription shares one per region, kept in a
# resource group of its own that outlives any one project.
SHARED_GROUP = "pdt-shared"
ENVIRONMENT_TYPE = "Microsoft.App/managedEnvironments"
PRICES_API = "https://prices.azure.com/api/retail/prices"
# The Retail Prices API rounds a converted price to four decimals, so a
# per-second price such as Container Apps vCPU becomes 0 in GBP. Prices stay
# in USD, and this meter (M416ms v2 in UK South, about 124 USD an hour),
# priced in both currencies, gives Azure's own exchange rate.
RATE_METER = "0238d90b-dcb1-5d36-a997-0b1612e97041"
ASSUMED_RUN_MINUTES = 5.0
RECENT_RUNS = 3
STORE_GROUP = "pdt-data"
STORE_ROLE = "Storage Blob Data Contributor"
SECRET_ROLE = "Key Vault Secrets Officer"
STORE_TAG_ARGS = tuple(f"{key}={value}" for key, value in STORE_TAGS.items())
STORE_METADATA_ARGS = tuple(
    f"{key.replace('-', '_')}={value}" for key, value in STORE_TAGS.items())
BLOB_ACTION = "Microsoft.Storage/storageAccounts/blobServices/containers/blobs"


def az(*args: str) -> subprocess.CompletedProcess:
    """Run one Azure CLI command in this process and return what it printed."""
    from azure.cli.core import get_default_cli
    from knack.log import cli_logger_names
    loggers = [logging.getLogger(name) for name in ("", *cli_logger_names)]
    before = [list(logger.handlers) for logger in loggers]
    out, err = io.StringIO(), io.StringIO()
    saved = sys.stdin, sys.stderr
    sys.stdin, sys.stderr = io.StringIO(), err
    try:
        try:
            code = get_default_cli().invoke(list(args), out_file=out)
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 1
    finally:
        sys.stdin, sys.stderr = saved
        # The CLI adds its log handlers once, on sys.stderr as it is then.
        # Removing them makes the next call write its errors to its own buffer.
        for logger, handlers in zip(loggers, before):
            for handler in logger.handlers[:]:
                if handler not in handlers:
                    logger.removeHandler(handler)
    return subprocess.CompletedProcess(args, code, out.getvalue(), err.getvalue())


def run_quiet(*args: str, retry_access: bool = False,
              retry_internal: bool = False, hints: dict[str, str] | None = None) -> str:
    waits = (10, 20, 40, 0) if retry_access or retry_internal else (0,)
    for wait in waits:
        proc = az(*args)
        if proc.returncode == 0:
            return proc.stdout
        output = proc.stderr.lower()
        access_error = retry_access and any(text in output for text in (
            "unable to fetch secret", "forbidden", "authorizationfailed",
            "does not have authorization",
        ))
        internal_error = retry_internal and "internalservererror" in output
        if proc.stderr.strip():
            console.say(console.escape(proc.stderr.strip()))
        if wait == 0 or not (access_error or internal_error):
            break
        reason = ("Azure returned an access error" if access_error
                  else "Azure returned InternalServerError")
        console.bullet(f"{reason}; retrying in {wait}s...", indent=4)
        time.sleep(wait)
    for text, hint in (hints or {}).items():
        if text.lower() in output:
            console.say(console.escape(hint))
    command = "pdt az " + " ".join(args[:4])
    fail(f"{console.value(command)} failed; fix the problem above and re-run")


LOCK_NAME = re.compile(r"Microsoft\.Authorization/locks/([^'\s,]+)", re.IGNORECASE)


def delete_unless_locked(*args: str) -> str:
    """Run an az delete. Return the lock names that refused it, or "" on success."""
    proc = az(*args)
    if proc.returncode == 0:
        return ""
    if "scopelocked" in proc.stderr.lower() or LOCK_NAME.search(proc.stderr):
        return ", ".join(sorted(set(LOCK_NAME.findall(proc.stderr)))) or "a lock"
    if proc.stderr.strip():
        console.say(console.escape(proc.stderr.strip()))
    command = "pdt az " + " ".join(args[:4])
    fail(f"{console.value(command)} failed; fix the problem above and re-run")


def az_json(*args: str):
    proc = az(*args, "--output", "json")
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


@dataclass(frozen=True)
class Environment:
    """The Container Apps environment a job runs in.

    `managed` means pdt created it and may delete it. A user-supplied one is
    only used: never created, tagged, checked for the tag, or deleted.
    """

    resource_group: str
    name: str
    managed: bool

    def resource_id(self, subscription: str) -> str:
        return (f"/subscriptions/{subscription}/resourceGroups/{self.resource_group}"
                f"/providers/{ENVIRONMENT_TYPE}/{self.name}")

    def __str__(self) -> str:
        return f"{self.resource_group}/{self.name}"


def parse_environment(value: str, region: str) -> Environment:
    if not value:
        return Environment(SHARED_GROUP, f"pdt-{region}", True)
    group, _, name = value.partition("/")
    return Environment(group, name, False)


def azure_settings(app: dict) -> dict:
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
        "environment": parse_environment(str(
            platform.get("environment")
            or os.environ.get("PDT_AZURE_CONTAINER_APPS_ENVIRONMENT") or ""), region),
        "identity": str(
            os.environ.get("PDT_AZURE_MANAGED_IDENTITY")
            or "pdt-runner"),
        # Log Analytics workspace names must be 4 to 63 characters, so this
        # default cannot be the bare "pdt" the other shared names start from.
        "workspace": str(
            os.environ.get("PDT_AZURE_LOG_WORKSPACE")
            or "pdt-logs"),
        **shared_names(subscription, resource_group),
    }


def shared_names(subscription: str, resource_group: str) -> dict[str, str]:
    # Storage, registry, vault, and Function App names are global, and the
    # resources live in one resource group, so two projects in one
    # subscription need different names.
    seed = f"{subscription}/{resource_group}" if subscription else resource_group
    suffix = hashlib.sha256(seed.encode()).hexdigest()[:10]
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


def preflight(app: dict, settings: dict) -> dict:
    requested = settings["subscription"]
    if requested == PLACEHOLDER_SUBSCRIPTION:
        requested = ""
    can_ask = can_prompt(None)
    account = az_json("account", "show")
    if not account:
        if not can_ask:
            login_command = "az login --service-principal -u <id> -p <secret> --tenant <tenant>"
            fail(f"no Azure sign-in on this computer; run `{console.value(login_command)}` "
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
    problem = subscription_problem(account)
    if problem:
        fail(console.escape(problem))
    settings["subscription"] = str(account["id"])
    # A first deploy learns the subscription here, and its names must match
    # every later deploy that reads the saved one.
    settings.update(shared_names(settings["subscription"], settings["resource_group"]))
    user = account.get("user") or {}
    is_user = str(user.get("type", "")).lower() == "user"
    deployer_id = os.environ.get("PDT_AZURE_DEPLOYER_OBJECT_ID", "").strip()
    if not deployer_id:
        deployer_id = object_id_from_token(access_token())
    if not deployer_id:
        fail("cannot determine the signed-in Azure principal; set "
             f"{console.value('PDT_AZURE_DEPLOYER_OBJECT_ID')}")
    settings["deployer_object_id"] = deployer_id
    settings["deployer_principal_type"] = "User" if is_user else "ServicePrincipal"
    return settings


# `az account show` reports the billing state. Azure rejects every write to a
# subscription that is not Enabled or PastDue, so deploy stops here instead of
# after the plan, at the first `az group create`.
WRITABLE_SUBSCRIPTION_STATES = ("enabled", "pastdue")
SUBSCRIPTIONS_URL = "https://portal.azure.com/#view/Microsoft_Azure_Billing/SubscriptionsBladeV2"


def subscription_problem(account: dict) -> str:
    state = str(account.get("state") or "")
    if not state or state.lower() in WRITABLE_SUBSCRIPTION_STATES:
        return ""
    return (f"Azure subscription {account.get('name')} ({account.get('id')}) is "
            f"{state}, so Azure rejects every change to it. Re-enable it at "
            f"{SUBSCRIPTIONS_URL}, or set platform.subscription in "
            f"{config.PROJECT_FILE} to another subscription id.")


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
    console.done(f"Saved subscription: {console.value(sub['id'])} to "
                 f"{console.value(saved.relative_to(config.find_project()))}.")


def choose_subscription(app: dict, requested: str, can_ask: bool) -> dict:
    available = az_json("account", "list", "--all") or []
    if not available:
        fail("your Azure account has no subscription yet; create one at "
             f"{console.value(SUBSCRIPTIONS_URL)}")
    for sub in available:
        if requested in (sub.get("id"), sub.get("name")):
            return sub
    console.warn(f"platform.subscription {console.value(repr(requested))} in "
                 f"{console.value('pdt.yml')} is not one of your subscriptions.")
    if not can_ask:
        fail(f"no Azure subscription selected; set {console.value('platform.subscription')} in "
             f"{console.value('pdt.yml')}, or set {console.value('PDT_AZURE_SUBSCRIPTION')}, "
             "to a subscription id this login can use")
    console.heading("Your Azure subscriptions:")
    for index, sub in enumerate(available, 1):
        console.choice(index, str(sub.get("name")), str(sub.get("id")))
    try:
        answer = input(f"Deploy to which one? [1-{len(available)}, or a subscription id] ").strip()
    except EOFError:
        answer = ""
    typed = [sub for sub in available if answer in (sub.get("id"), sub.get("name"))]
    if answer.isdigit() and 1 <= int(answer) <= len(available):
        sub = available[int(answer) - 1]
    elif typed:
        sub = typed[0]
    else:
        fail("no Azure subscription selected; type a number from the list or one of the subscription ids")
    save_subscription(app, sub)
    return sub


def login(requested: str) -> None:
    console.status("Opening your browser for the Azure login...")
    proc = subprocess.run([*AZ, "login"], capture_output=True, text=True)
    if proc.returncode == 0:
        return
    output = proc.stdout + proc.stderr
    if "No subscriptions found" not in output:
        console.say(console.escape(output.strip()))
        fail(f"{console.value('pdt az login')} failed; fix the problem above and re-run")
    user = re.search(r"No subscriptions found for (\S+)\.", output)
    who = user.group(1) if user else "your Azure account"
    console.warn(f"The login worked, but {console.value(who)} has no Azure subscription.")
    console.say("Azure bills every resource to a subscription, so deploy cannot continue without one.")
    console.bullet(f"1. Create one at {console.value(SUBSCRIPTIONS_URL)}")
    console.bullet("(an Azure free account also works: "
                   f"{console.value('https://azure.microsoft.com/free')}).", indent=5)
    console.bullet(f"2. Put its Subscription ID in {console.value('platform.subscription')} in "
                   f"{console.value('pdt.yml')}.")
    console.bullet("3. Run the same command again.")
    if requested in output:
        console.note(f"{console.value(requested)} in {console.value('pdt.yml')} is your tenant "
                     "(directory) id, not a subscription id.")
    raise SystemExit(1)


def relogin(requested: str) -> int:
    console.status("Clearing the cached Azure login on this computer...")
    az("account", "clear")
    console.say("Choose a different account in the browser to sign in as someone else.")
    login(requested)
    account = az_json("account", "show")
    if not account:
        fail("Azure login failed")
    user = (account.get("user") or {}).get("name") or "unknown"
    console.done(f"Signed in as {console.value(user)}")
    console.field("Subscription", f"{account.get('name')} ({account.get('id')})")
    return 0


def resource_id(settings: dict[str, str], provider: str, kind: str, name: str) -> str:
    return (f"/subscriptions/{settings['subscription']}/resourceGroups/"
            f"{settings['resource_group']}/providers/{provider}/{kind}/{name}")


def secret_name(app_name: str) -> str:
    return clean_name(f"pdt-{app_name}-env", 127)


def secret_scope(settings: dict[str, str], sid: str) -> str:
    return resource_id(settings, "Microsoft.KeyVault", "vaults", settings["vault"]) + f"/secrets/{sid}"


def set_key_vault_secret(vault: str, name: str, payload: str, app_name: str) -> str:
    fd, filename = tempfile.mkstemp(prefix="pdt-secret-")
    try:
        os.chmod(filename, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(payload)
        return run_quiet(
            "keyvault", "secret", "set", "--vault-name", vault, "--name", name,
            "--file", filename, "--encoding", "utf-8", "--tags",
            "managed-by=pdt", f"pdt-app={app_name}",
            "--query", "id", "--output", "tsv", retry_access=True).strip()
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
                 product: str = "", currency: str = "USD") -> tuple[float, str]:
    query = (f"serviceName eq '{service}' and armRegionName eq '{region}' "
             f"and meterName eq '{meter}' and skuName eq '{sku}' "
             f"and type eq 'Consumption'")
    if product:
        query += f" and productName eq '{product}'"
    url = f"{PRICES_API}?currencyCode='{currency}'&$filter={urllib.parse.quote(query)}"
    items = fetch_json(url, timeout=30).get("Items") or []
    items = [i for i in items if i.get("retailPrice")
             and i.get("currencyCode", currency) == currency]
    if not items:
        raise LookupError(f"no {meter!r} {currency} price for {service} in region {region}")
    return float(items[0]["retailPrice"]), items[0].get("unitOfMeasure", "")


def exchange_rate(currency: str) -> float:
    """What one USD costs in `currency`, at the rate Azure prices with today."""
    if currency == "USD":
        return 1.0
    query = urllib.parse.quote(f"meterId eq '{RATE_METER}' and type eq 'Consumption'")
    prices = []
    for code in ("USD", currency):
        items = fetch_json(f"{PRICES_API}?currencyCode='{code}'&$filter={query}",
                           timeout=30).get("Items") or []
        if not items or not items[0].get("retailPrice"):
            raise LookupError(f"no {code} price for the exchange-rate meter {RATE_METER}")
        prices.append(float(items[0]["retailPrice"]))
    return prices[1] / prices[0]


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
            delete_command = f"pdt az resource delete --ids {resource.get('id')}"
            fail(f"an earlier deploy created the {label} {console.value(resource['name'])} in "
                 "Azure. "
                 f"This deploy would create a second one, {console.value(settings[key])}, and "
                 f"leave the first one unused. This happens when the subscription or the "
                 f"resource group in {console.value(config.PROJECT_FILE)} changed after that "
                 f"deploy.\n"
                 f"If other apps still use {console.value(resource['name'])}, put the earlier "
                 f"subscription and resource group back in "
                 f"{console.value(config.PROJECT_FILE)}.\n"
                 f"If nothing uses it, remove it and deploy again:\n"
                 f"  {console.value(delete_command)}")


def secret_state(settings: dict[str, str], sid: str, app_name: str,
                 wanted: bool) -> tuple[bool, str | None]:
    vault = az_json("keyvault", "show", "--name", settings["vault"],
                    "--resource-group", settings["resource_group"])
    require_managed(vault, f"Key Vault {console.value(settings['vault'])}")
    vault_exists = vault is not None
    current = None
    if vault_exists and wanted:
        current = az_json("keyvault", "secret", "show", "--vault-name",
                          settings["vault"], "--name", sid)
        if current and not managed_secret(settings, sid, app_name):
            fail(f"Key Vault secret {console.value(sid)} already exists but is not owned "
                 f"by PDT app {console.value(app_name)}; choose another Key Vault")
    return vault_exists, current.get("value") if current else None


def secret_actions(sid: str, values: dict, current: str | None,
                   payload: str) -> list[console.Markup]:
    if not values:
        return []
    state = "unchanged" if current == payload else (
        "update" if current else "create")
    return [console.Markup(f"{state} Key Vault secret {console.value(sid)} "
                           f"({secret_contents(values)})")]


def register_providers(names: tuple[str, ...]) -> None:
    pending = [name for name in names
               if az_tsv("provider", "show", "--namespace", name,
                         "--query", "registrationState") != "Registered"]
    if not pending:
        return
    console.step("registering Azure providers: "
                 f"{', '.join(console.value(name) for name in pending)}")
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
            console.bullet(f"still waiting after {waited}s for: "
                           f"{', '.join(console.value(name) for name in pending)}", indent=4)


def ensure_group_and_vault(settings: dict[str, str], providers: tuple[str, ...],
                           vault_exists: bool) -> str:
    rg = settings["resource_group"]
    group = az_json("group", "show", "--name", rg)
    require_managed(group, f"resource group {console.value(rg)}")
    register_providers((*COMMON_PROVIDERS, *providers))
    console.step(f"reconciling resource group {console.value(rg)}")
    run_quiet("group", "create", "--name", rg, "--location", settings["region"],
              "--tags", "managed-by=pdt")
    if not vault_exists:
        purge_deleted_vault(settings)
        console.step(f"creating Key Vault {console.value(settings['vault'])}")
        run_quiet("keyvault", "create", "--name", settings["vault"],
                  "--resource-group", rg, "--location", settings["region"],
                  "--enable-rbac-authorization", "true",
                  "--tags", "managed-by=pdt")
    vault_id = resource_id(settings, "Microsoft.KeyVault", "vaults", settings["vault"])
    assign_role(vault_id, settings["deployer_object_id"], "Key Vault Secrets Officer",
                settings["deployer_principal_type"])
    return vault_id


def purge_deleted_vault(settings: dict[str, str]) -> None:
    """A soft-deleted vault still owns its name, so create fails until it is purged."""
    deleted = az_json("keyvault", "show-deleted", "--name", settings["vault"])
    if not deleted:
        return
    tags = (deleted.get("properties") or {}).get("tags") or {}
    if tags.get("managed-by") != "pdt":
        fail(f"a soft-deleted Key Vault named {console.value(settings['vault'])} exists but is not "
             "managed by PDT; purge it or deploy to another subscription")
    console.step(f"purging soft-deleted Key Vault {console.value(settings['vault'])}")
    run_quiet("keyvault", "purge", "--name", settings["vault"])


def workspace_resource(settings: dict) -> dict | None:
    return az_json("monitor", "log-analytics", "workspace", "show",
                   "--resource-group", settings["environment"].resource_group,
                   "--workspace-name", settings["workspace"])


def ensure_workspace(settings: dict, exists: bool) -> tuple[str, str]:
    group = settings["environment"].resource_group
    if not exists:
        console.step(f"creating Log Analytics workspace {console.value(settings['workspace'])}")
        run_quiet("monitor", "log-analytics", "workspace", "create",
                  "--resource-group", group, "--workspace-name", settings["workspace"],
                  "--location", settings["region"], "--tags", "managed-by=pdt")
    logs_id = az_tsv("monitor", "log-analytics", "workspace", "show",
                     "--resource-group", group, "--workspace-name",
                     settings["workspace"], "--query", "customerId")
    logs_key = az_tsv("monitor", "log-analytics", "workspace", "get-shared-keys",
                      "--resource-group", group, "--workspace-name",
                      settings["workspace"], "--query", "primarySharedKey")
    return logs_id, logs_key


def ensure_shared_group(settings: dict) -> None:
    group = settings["environment"].resource_group
    require_managed(az_json("group", "show", "--name", group),
                    f"resource group {console.value(group)}")
    console.step(f"reconciling resource group {console.value(group)}")
    run_quiet("group", "create", "--name", group, "--location", settings["region"],
              "--tags", "managed-by=pdt")


def ensure_secret(settings: dict[str, str], sid: str, values: dict,
                  payload: str, current: str | None, app_name: str) -> str | None:
    """Write the secret when it changed; return its URI without a version.

    A URI that names a version pins the job to that version, so a later
    change in the vault never reaches a run.
    """
    if not values:
        return None
    if current != payload:
        console.step(f"writing Key Vault secret {console.value(sid)}")
        uri = set_key_vault_secret(settings["vault"], sid, payload, app_name)
    else:
        uri = az_tsv("keyvault", "secret", "show", "--vault-name", settings["vault"],
                     "--name", sid, "--query", "id")
    return uri.rsplit("/", 1)[0]


def disable_old_secret_versions(settings: dict[str, str], sid: str) -> None:
    """Key Vault cannot delete one version, so every version but the latest is disabled."""
    versions = az_json("keyvault", "secret", "list-versions", "--vault-name",
                       settings["vault"], "--name", sid) or []
    versions.sort(key=lambda version: version["attributes"]["created"])
    for version in versions[:-1]:
        if version["attributes"]["enabled"]:
            run_quiet("keyvault", "secret", "set-attributes", "--id", version["id"],
                      "--enabled", "false", retry_access=True)


def managed_secret(settings: dict[str, str], sid: str, app_name: str) -> bool:
    # Tags belong to a version, and a version added in the portal has none.
    versions = az_json("keyvault", "secret", "list-versions", "--vault-name",
                       settings["vault"], "--name", sid) or []
    return any(owned_by(version, app_name) for version in versions)


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
SMART_ACTION_SHORT_NAME = "SmartDetect"
# Azure's own copy of the action group emails these two built-in roles, so
# pdt's copy names them too and nobody loses a notification.
SMART_ACTION_ROLES = (
    ("MonitoringContributor", "749f88d5-cbae-40b8-bcfc-e573ddc772fa"),
    ("MonitoringReader", "43d0d8ad-25c7-4714-9337-8ba259a9fe05"),
)


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


def ensure_action_group(rg: str) -> None:
    """Create the Smart Detection action group before Azure gets to it.

    Azure adds this group beside the first Application Insights component in
    the subscription, minutes after the component and never on request. A
    deploy that only tagged what it found had nothing to tag yet, so the group
    stayed untagged and destroy could not claim it. Creating it first, under
    the name Azure looks for, leaves one tagged group both sides use.
    """
    if side_resource(rg, SMART_ACTION_GROUP, ACTION_GROUP_TYPE):
        return
    receivers = []
    for name, role in SMART_ACTION_ROLES:
        receivers += ["--action", "armrole", name, role]
    console.step(f"creating action group {console.value(SMART_ACTION_GROUP)}")
    run_quiet("monitor", "action-group", "create", "--name", SMART_ACTION_GROUP,
              "--resource-group", rg, "--short-name", SMART_ACTION_SHORT_NAME,
              *receivers, "--tags", "managed-by=pdt")


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


def destroy_group(settings: dict[str, str], app_name: str) -> bool:
    """Delete the project's group. False means another app holds it, so it stays."""
    rg = settings["resource_group"]
    others = other_pdt_apps(rg, app_name)
    if others:
        console.note(f"kept: resource group {console.value(rg)} (apps deployed since the plan: "
                     f"{', '.join(console.value(other) for other in others)})")
        return False
    console.step(f"deleting resource group {console.value(rg)} (takes a few minutes)")
    locked = delete_unless_locked("group", "delete", "--name", rg, "--yes")
    if locked:
        console.note(f"kept: resource group {console.value(rg)} with its ACR and Key Vault "
                     f"(locked by {console.value(locked)})")
        return False
    if az_json("keyvault", "show-deleted", "--name", settings["vault"]):
        console.step(f"purging soft-deleted Key Vault {console.value(settings['vault'])}")
        run_quiet("keyvault", "purge", "--name", settings["vault"])
    if az_tsv("group", "exists", "--name", rg) == "false":
        console.done(f"Nothing remains in resource group {console.value(rg)}.")
    return True


def report_shared_kept(rg: str, others: list[str]) -> None:
    if others:
        console.note(f"apps still deployed in resource group {console.value(rg)}: "
                     f"{', '.join(console.value(other) for other in others)}. "
                     "Shared resources stay until the last app is destroyed.")
    else:
        console.note(f"resource group {console.value(rg)} is not fully owned by PDT, so PDT "
                     "kept it.")
    list_remaining(rg)


def list_remaining(rg: str) -> None:
    console.heading("Still present:")
    for resource in az_json("resource", "list", "--resource-group", rg) or []:
        console.bullet(f"{console.value(resource.get('name'))}  "
                       f"({console.escape(str(resource.get('type')))})")


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


def store_description(store: dict[str, str]) -> console.Markup:
    return console.Markup(f"storage account {console.value(store['account'])}, "
                          f"container {console.value(store['container'])}")


def store_exists(store: dict[str, str]) -> bool:
    group = az_json("group", "show", "--name", store["group"])
    require_managed(group, f"resource group {console.value(store['group'])}")
    account = az_json("storage", "account", "show", "--name", store["account"],
                      "--resource-group", store["group"])
    require_managed(account, f"Storage account {console.value(store['account'])}")
    if account is None:
        return False
    container = az_json("storage", "container-rm", "show",
                        "--storage-account", store["account"],
                        "--resource-group", store["group"], "--name", store["container"])
    if container is None:
        return False
    metadata = container.get("metadata") or {}
    if metadata and metadata.get("managed_by") != "pdt":
        fail(f"container {console.value(store['container'])} exists but is not managed by PDT")
    return bool(metadata)


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
               identity: str) -> list[console.Markup]:
    return store_plan_lines(store_description(store), exists, identity, app_name) + [
        console.Markup("grant the signed-in Azure account write access to "
                       f"{console.value(store['container'])} (for pdt storage)"),
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
        container = az_json("storage", "container-rm", "show",
                            "--storage-account", store["account"],
                            "--resource-group", store["group"], "--name", store["container"])
        run_quiet("storage", "container-rm", "update" if container else "create",
                  "--storage-account", store["account"],
                  "--resource-group", store["group"], "--name", store["container"],
                  "--metadata", *STORE_METADATA_ARGS)
    assign_role(store["container_id"], settings["deployer_object_id"], STORE_ROLE,
                settings["deployer_principal_type"])


def store_cost(usage: tuple[int, int], region: str, currency: str = "USD") -> tuple[str, float]:
    count, size = usage
    price, _ = retail_price(region, "Storage", "Hot LRS Data Stored", "Hot LRS",
                            "General Block Blob v2", currency)
    return store_cost_label(count, size), size / 1024 ** 3 * price


def deployer_store(settings: dict[str, str], app_name: str) -> Store:
    from azure.identity import AzureCliCredential
    os.environ["PATH"] = os.pathsep.join(
        [str(Path(sys.executable).parent), os.environ.get("PATH", "")])
    return Store(store_url(store_settings(settings), app_name), AzureCliCredential())


def store_usage(deployer: Store) -> tuple[int, int] | None:
    """None until this login holds the data role, which ensure_store grants."""
    try:
        return deployer.usage()
    except Exception as exc:  # the SDK raises HttpResponseError; tests have no SDK
        if "AuthorizationPermissionMismatch" not in str(exc):
            raise
        console.note("this login cannot read the data store yet; the deploy grants it "
                     "the role, and the next plan shows the stored data")
        return None


def storage(app: dict, settings: dict[str, str], rest: list[str], assume_yes: bool) -> int:
    return storage_cli.run(deployer_store(settings, app["name"]), app, rest, assume_yes)


def run_basis(seconds: float | None) -> tuple[float, str]:
    if seconds is None:
        return ASSUMED_RUN_MINUTES * 60, f"{ASSUMED_RUN_MINUTES:g} min assumed"
    return seconds, f"{seconds / 60:.1f} min avg of recent runs"


def key_vault_item(region: str, runs: float, currency: str = "USD") -> tuple[str, float]:
    kv_price, _ = retail_price(region, "Key Vault", "Operations", "Standard", currency=currency)
    return f"Key Vault: 1 secret, ~{runs:.0f} reads", runs * kv_price / 10000


def cost_estimate(region: str, items: list[tuple[str, float]],
                  excludes: str, currency: str, converted: str = "") -> CostEstimate:
    return CostEstimate(items, f"{region} list prices{converted}, before free grants",
                        excludes, currency)


def load_app(app_name: str) -> dict:
    try:
        app = config.merged_app(app_name)
    except config.ConfigError as exc:
        fail(console.escape(str(exc)))
    config.load_env(app["dir"])
    return app


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "az":
        return subprocess.run([*AZ, *sys.argv[2:]]).returncode
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command",
                        choices=("deploy", "destroy", "login", "storage", "secrets", "runs", "logs",
                                 "pause", "unpause", "start"))
    parser.add_argument("app")
    parser.add_argument("rest", nargs="*")
    parser.add_argument("--yes", action="store_true")
    args = parser.parse_intermixed_args()
    app = load_app(args.app)
    if args.command == "login":
        requested = azure_settings(app)["subscription"]
        return relogin("" if requested == PLACEHOLDER_SUBSCRIPTION else requested)
    if args.command == "storage":
        return storage(app, preflight(app, azure_settings(app)), args.rest, args.yes)
    from pdt import deploy_azure_container_apps as module
    if args.command == "runs":
        return module.runs(app, preflight(app, azure_settings(app)), args.rest)
    if args.command == "logs":
        return module.logs(app, preflight(app, azure_settings(app)), args.rest)
    if args.command in ("pause", "unpause"):
        return module.pause(app, preflight(app, azure_settings(app)), args.command == "pause")
    if args.command == "start":
        return module.start(app, preflight(app, azure_settings(app)))
    if app["timezone"] not in ("Etc/UTC", "UTC"):
        fail("Azure evaluates cron schedules only in UTC; set "
             f"{console.value('timezone: Etc/UTC')}")
    if args.command == "secrets":
        return module.secrets(app, args.rest[0], args.yes, *args.rest[1:])
    if args.command == "deploy":
        return module.deploy(app, args.yes)
    return module.destroy(app, args.yes)


if __name__ == "__main__":
    sys.exit(main())
