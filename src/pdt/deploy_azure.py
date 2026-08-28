#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "azure-cli==2.89.1",
#     "pyyaml",
#     "python-dotenv",
# ]
#
# [tool.uv]
# prerelease = "allow"
# ///
"""Deploy an app to Azure.

Shared login, subscription, resource group, Key Vault, price, and cost code
lives here. The runtime-specific parts are in deploy_azure_functions.py
(platform.runtime: functions, the default) and deploy_azure_container_apps.py.

The Azure CLI is a Python package, so the script header installs it and
every call here runs it as `python -m azure.cli`. No system install is
needed. Login state lives in ~/.azure either way. azure-cli pins a few of
its own dependencies to pre-release versions, so the header allows
pre-releases; without that, uv before 0.12 refuses to resolve it.

Every app owns one tagged Key Vault secret holding its env vars as one
json blob, exposed to the job as PDT_ENV_JSON. Secret values are sent to
Azure through a protected temporary file, never on the command line.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pdt import config
from pdt.deploy_common import fail

AZ = [sys.executable, "-m", "azure.cli"]
COMMON_PROVIDERS = ("Microsoft.KeyVault", "Microsoft.ManagedIdentity")
PLACEHOLDER_SUBSCRIPTION = "00000000-0000-0000-0000-000000000000"
PRICES_API = "https://prices.azure.com/api/retail/prices"
ASSUMED_RUN_MINUTES = 5.0
RECENT_RUNS = 3


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
        print(f"    Azure RBAC is still propagating; retrying in {wait}s...")
        time.sleep(wait)
    print(proc.stderr.strip())
    fail(f"az {' '.join(args[:4])} failed; fix the problem above and re-run")


def run_stream(*args: str) -> None:
    proc = subprocess.run([*AZ, *args])
    if proc.returncode != 0:
        fail(f"az {' '.join(args[:3])} failed; fix the problem above and re-run")


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
    seed = subscription or resource_group
    suffix = hashlib.sha256(seed.encode()).hexdigest()[:10]
    return {
        "subscription": subscription,
        "region": region,
        "resource_group": resource_group,
        "suffix": suffix,
        "environment": str(
            os.environ.get("PDT_AZURE_CONTAINER_APPS_ENVIRONMENT")
            or "pdt"),
        "registry": str(
            os.environ.get("PDT_AZURE_CONTAINER_REGISTRY")
            or f"pdt{suffix}")[:50].replace("-", ""),
        "storage": str(
            os.environ.get("PDT_AZURE_STORAGE_ACCOUNT")
            or f"pdt{suffix}")[:24].replace("-", ""),
        "vault": str(
            os.environ.get("PDT_AZURE_KEY_VAULT")
            or f"pdt-{suffix}")[:24].strip("-"),
        "identity": str(
            os.environ.get("PDT_AZURE_MANAGED_IDENTITY")
            or "pdt-runner"),
        # Log Analytics workspace names must be 4 to 63 characters, so this
        # default cannot be the bare "pdt" the other shared names start from.
        "workspace": str(
            os.environ.get("PDT_AZURE_LOG_WORKSPACE")
            or "pdt-logs"),
    }


def preflight(app: dict, settings: dict[str, str]) -> dict[str, str]:
    requested = settings["subscription"]
    if requested == PLACEHOLDER_SUBSCRIPTION:
        requested = ""
    account = az_json("account", "show")
    if not account:
        print("You are not logged in to Azure yet.")
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
    if requested not in (account.get("id"), account.get("name")):
        account = choose_subscription(app, requested)
        run_quiet("account", "set", "--subscription", account["id"])
    settings["subscription"] = str(account["id"])
    user = account.get("user") or {}
    is_user = str(user.get("type", "")).lower() == "user"
    deployer_id = os.environ.get("PDT_AZURE_DEPLOYER_OBJECT_ID", "").strip()
    if not deployer_id:
        if is_user:
            deployer_id = az_tsv("ad", "signed-in-user", "show", "--query", "id")
        else:
            deployer_id = az_tsv(
                "ad", "sp", "show", "--id", str(user.get("name") or ""),
                "--query", "id")
    if not deployer_id:
        fail("cannot determine the signed-in Azure principal; set "
             "PDT_AZURE_DEPLOYER_OBJECT_ID")
    settings["deployer_object_id"] = deployer_id
    settings["deployer_principal_type"] = "User" if is_user else "ServicePrincipal"
    return settings


def choose_subscription(app: dict, requested: str) -> dict:
    available = az_json("account", "list", "--all") or []
    if not available:
        fail("your Azure account has no subscription yet; create one at "
             "https://portal.azure.com/#view/Microsoft_Azure_Billing/SubscriptionsBladeV2")
    for sub in available:
        if requested in (sub.get("id"), sub.get("name")):
            return sub
    if requested:
        print(f"platform.subscription {requested!r} in pdt.yml is not one of your subscriptions.")
    print("Your Azure subscriptions:")
    for index, sub in enumerate(available, 1):
        print(f"  {index}. {sub.get('name')}  {sub.get('id')}")
    try:
        answer = input(f"Deploy to which one? [1-{len(available)}] ").strip()
    except EOFError:
        answer = ""
    if not answer.isdigit() or not 1 <= int(answer) <= len(available):
        fail("no Azure subscription selected")
    sub = available[int(answer) - 1]
    saved = config.save_platform_key(app, "subscription", sub["id"])
    print(f"Saved subscription: {sub['id']} to {saved.relative_to(config.find_project())}.")
    return sub


def login(requested: str) -> None:
    print("Opening your browser for the Azure login...")
    proc = subprocess.run([*AZ, "login"], capture_output=True, text=True)
    if proc.returncode == 0:
        return
    output = proc.stdout + proc.stderr
    if "No subscriptions found" not in output:
        print(output.strip())
        fail("az login failed; fix the problem above and re-run")
    user = re.search(r"No subscriptions found for (\S+)\.", output)
    who = user.group(1) if user else "your Azure account"
    print(f"The login worked, but {who} has no Azure subscription.")
    print("Azure bills every resource to a subscription, so deploy cannot continue without one.")
    print("  1. Create one at https://portal.azure.com/#view/Microsoft_Azure_Billing/SubscriptionsBladeV2")
    print("     (an Azure free account also works: https://azure.microsoft.com/free).")
    print("  2. Put its Subscription ID in platform.subscription in pdt.yml.")
    print("  3. Run the same command again.")
    if requested in output:
        print(f"Note: {requested} in pdt.yml is your tenant (directory) id, not a subscription id.")
    raise SystemExit(1)


def relogin(requested: str) -> int:
    print("Clearing the cached Azure login on this computer...")
    subprocess.run([*AZ, "account", "clear"], stdin=subprocess.DEVNULL,
                   capture_output=True, text=True)
    print("Choose a different account in the browser to sign in as someone else.")
    login(requested)
    account = az_json("account", "show")
    if not account:
        fail("Azure login failed")
    print(f"Signed in as {(account.get('user') or {}).get('name') or 'unknown'}")
    print(f"Subscription {account.get('name')} ({account.get('id')})")
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
                principal_type: str = "ServicePrincipal") -> None:
    existing = az_json(
        "role", "assignment", "list", "--assignee", principal_id,
        "--role", role, "--scope", scope)
    if existing:
        return
    run_quiet("role", "assignment", "create", "--assignee-object-id", principal_id,
              "--assignee-principal-type", principal_type, "--role", role,
              "--scope", scope)


def retail_price(region: str, service: str, meter: str, sku: str) -> tuple[float, str]:
    query = (f"serviceName eq '{service}' and armRegionName eq '{region}' "
             f"and meterName eq '{meter}' and skuName eq '{sku}' "
             f"and type eq 'Consumption'")
    url = f"{PRICES_API}?$filter={urllib.parse.quote(query)}"
    with urllib.request.urlopen(url, timeout=30) as resp:
        items = json.load(resp).get("Items") or []
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
    print(f"==> registering Azure providers: {', '.join(pending)}")
    print("    (a new subscription can take several minutes for this)")
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
            print(f"    still waiting after {waited}s for: {', '.join(pending)}")


def ensure_group_and_vault(settings: dict[str, str], providers: tuple[str, ...],
                           vault_exists: bool) -> str:
    rg = settings["resource_group"]
    group = az_json("group", "show", "--name", rg)
    require_managed(group, f"resource group {rg}")
    register_providers((*COMMON_PROVIDERS, *providers))
    print(f"==> reconciling resource group {rg}")
    run_quiet("group", "create", "--name", rg, "--location", settings["region"],
              "--tags", "managed-by=pdt")
    if not vault_exists:
        print(f"==> creating Key Vault {settings['vault']}")
        run_quiet("keyvault", "create", "--name", settings["vault"],
                  "--resource-group", rg, "--location", settings["region"],
                  "--enable-rbac-authorization", "true",
                  "--tags", "managed-by=pdt")
    if not workspace_exists(settings):
        print(f"==> creating Log Analytics workspace {settings['workspace']}")
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
        print(f"==> writing Key Vault secret {sid}")
        return set_key_vault_secret(settings["vault"], sid, payload, digest, app_name)
    return az_tsv("keyvault", "secret", "show", "--vault-name", settings["vault"],
                  "--name", sid, "--query", "id")


def managed_secret(settings: dict[str, str], sid: str, app_name: str) -> bool:
    secret = az_json("keyvault", "secret", "show", "--vault-name",
                     settings["vault"], "--name", sid)
    return owned_by(secret, app_name)


def delete_secret(settings: dict[str, str], sid: str) -> None:
    run_quiet("keyvault", "secret", "delete", "--vault-name",
              settings["vault"], "--name", sid)


def other_pdt_apps(rg: str, exclude_app: str) -> list[str]:
    resources = az_json("resource", "list", "--resource-group", rg) or []
    names = set()
    for resource in resources:
        tags = resource.get("tags") or {}
        if tags.get("managed-by") == "pdt" and tags.get("pdt-app"):
            names.add(tags["pdt-app"])
    names.discard(exclude_app)
    return sorted(names)


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
    return all(managed_by_pdt(resource) for resource in resources)


def purge_secret(settings: dict[str, str], sid: str) -> None:
    # No recovery windows: purge right after the soft delete lands.
    delete_secret(settings, sid)
    for _ in range(30):
        if az_json("keyvault", "secret", "show-deleted", "--vault-name",
                   settings["vault"], "--name", sid):
            break
        time.sleep(2)
    run_quiet("keyvault", "secret", "purge", "--vault-name",
              settings["vault"], "--name", sid)


def destroy_group(settings: dict[str, str]) -> None:
    rg = settings["resource_group"]
    print(f"==> deleting resource group {rg} (takes a few minutes)")
    run_quiet("group", "delete", "--name", rg, "--yes")
    if az_json("keyvault", "show-deleted", "--name", settings["vault"]):
        print(f"==> purging soft-deleted Key Vault {settings['vault']}")
        run_quiet("keyvault", "purge", "--name", settings["vault"])
    if az_tsv("group", "exists", "--name", rg) == "false":
        print("Nothing remains.")


def report_shared_kept(rg: str, others: list[str]) -> None:
    if others:
        print(f"Apps still deployed in resource group {rg}: {', '.join(others)}.")
        print("Shared resources stay until the last app is destroyed.")
    else:
        print(f"Resource group {rg} is not fully owned by PDT, so PDT kept it.")
    print("Still present:")
    for resource in az_json("resource", "list", "--resource-group", rg) or []:
        print(f"  {resource.get('name')}  ({resource.get('type')})")


def run_basis(seconds: float | None) -> tuple[float, str]:
    if seconds is None:
        return ASSUMED_RUN_MINUTES * 60, f"{ASSUMED_RUN_MINUTES:g} min assumed"
    return seconds, f"{seconds / 60:.1f} min avg of recent runs"


def key_vault_item(region: str, runs: float) -> tuple[str, float]:
    kv_price, _ = retail_price(region, "Key Vault", "Operations", "Standard")
    return f"Key Vault: 1 secret, ~{runs:.0f} reads", runs * kv_price / 10000


def cost_estimate_lines(region: str, items: list[tuple[str, float]],
                        excludes: str) -> list[str]:
    total = sum(cost for _, cost in items)
    width = max(len(label) for label, _ in items)
    lines = [f"Estimated monthly cost ({region} list prices, before free grants):"]
    for label, cost in items:
        lines.append(f"  {label:<{width}}  ${cost:>7.2f}")
    lines.append(f"  {'total':<{width}}  ${total:>7.2f}")
    lines.append(f"  ({excludes})")
    return lines


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
    parser.add_argument("command", choices=("deploy", "destroy", "login"))
    parser.add_argument("app")
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--profile", help="not used by Azure")
    args = parser.parse_args()
    app = load_app(args.app)
    if args.command == "login":
        requested = azure_settings(app)["subscription"]
        return relogin("" if requested == PLACEHOLDER_SUBSCRIPTION else requested)
    if app["timezone"] not in ("Etc/UTC", "UTC"):
        fail("Azure evaluates cron schedules only in UTC; set timezone: Etc/UTC")
    runtime = app["platform"].get("runtime", "functions")
    if runtime == "container_apps":
        from pdt import deploy_azure_container_apps as module
    else:
        from pdt import deploy_azure_functions as module
    if args.command == "deploy":
        return module.deploy(app, args.yes)
    return module.destroy(app, args.yes)


if __name__ == "__main__":
    sys.exit(main())
