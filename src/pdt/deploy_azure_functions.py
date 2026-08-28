"""Deploy an app as a scheduled Azure Functions timer trigger.

Selected with platform.runtime: functions (the default). Entered through
deploy_azure.py, which owns the uv script header, login, and Key Vault.
The resource group, Storage account, and Key Vault are shared. Each app
owns one tagged Function App on the Flex Consumption plan, built remotely
from a zip of the app plus a generated function_app.py.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import zipfile
from pathlib import Path

from pdt import config
from pdt.deploy import confirm
from pdt.deploy_azure import (
    RECENT_RUNS, assign_role, az_json, azure_settings, clean_name, cost_estimate_lines,
    destroy_group, ensure_group_and_vault, ensure_secret, group_can_be_deleted,
    key_vault_item, managed_secret, other_pdt_apps, owned_by, preflight,
    purge_secret, report_shared_kept, require_managed,
    retail_price, run_basis, run_quiet, run_stream, secret_actions,
    secret_name, secret_state, workspace_resource,
)
from pdt.deploy_common import fail, gather_secrets, run_build, stage_build_context

PROVIDERS = ("Microsoft.Web", "Microsoft.Storage")
INSTANCE_MEMORY_MB = 512
PYTHON_VERSION = "3.12"
HOST_JSON = {
    "version": "2.0",
    "functionTimeout": "00:30:00",
    "extensionBundle": {
        "id": "Microsoft.Azure.Functions.ExtensionBundle",
        "version": "[4.*, 5.0.0)",
    },
}
FUNCTION_APP = """\
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import azure.functions as func

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("PDT_PROJECT", str(ROOT))
app = func.FunctionApp()
_APP = None


def _app():
    global _APP
    if _APP is None:
        spec = importlib.util.spec_from_file_location("pdt_app", ROOT / {app_name!r} / "run.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _APP = module
    return _APP


@app.timer_trigger(schedule="%PDT_SCHEDULE%", arg_name="timer", run_on_startup=False)
def run(timer: func.TimerRequest) -> None:
    for name, value in json.loads(os.environ.get("PDT_ENV_JSON") or "{{}}").items():
        os.environ.setdefault(name, str(value))
    status = _app().main()
    if status:
        raise RuntimeError(f"app exited with status {{status}}")
"""


def function_app_name(settings: dict[str, str], app_name: str) -> str:
    # A Function App name is a public hostname, so it carries the subscription suffix.
    return clean_name(f"pdt-{app_name}-{settings['suffix'][:6]}", 60)


def ncrontab(cron: str) -> str:
    # Azure timers use six fields; the first is seconds.
    return f"0 {cron}"


def build_package(app: dict) -> Path:
    stage = stage_build_context(app)
    work = Path(tempfile.mkdtemp(prefix="pdt-functions-"))
    archive = work / "function.zip"
    try:
        (stage / "function_app.py").write_text(FUNCTION_APP.format(app_name=app["name"]))
        (stage / "host.json").write_text(json.dumps(HOST_JSON, indent=2))
        run_build([
            "uv", "export", "--script", str(app["dir"] / "run.py"),
            "--format", "requirements-txt", "--no-hashes",
            "--output-file", str(stage / "requirements.txt"),
        ])
        with (stage / "requirements.txt").open("a") as handle:
            handle.write("azure-functions\n")
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
            for path in sorted(item for item in stage.rglob("*") if item.is_file()):
                zf.write(path, path.relative_to(stage).as_posix())
        return archive
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def average_run_seconds(function_id: str) -> float | None:
    # Flex Consumption keeps no per-run list; the hourly metrics give
    # execution units (MB-ms) and counts. The last few non-empty hours
    # approximate the last few runs.
    data = az_json(
        "monitor", "metrics", "list", "--resource", function_id,
        "--metric", "FunctionExecutionUnits,FunctionExecutionCount",
        "--interval", "PT1H", "--offset", "7d", "--aggregation", "Total") or {}
    series = {m["name"]["value"]: (m.get("timeseries") or [{}])[0].get("data") or []
              for m in data.get("value") or []}
    units = series.get("FunctionExecutionUnits", [])
    counts = series.get("FunctionExecutionCount", [])
    hours = [(u.get("total") or 0, c.get("total") or 0)
             for u, c in zip(units, counts) if (c.get("total") or 0) > 0]
    recent = hours[-RECENT_RUNS:]
    if not recent:
        return None
    total_units = sum(u for u, _ in recent)
    total_count = sum(c for _, c in recent)
    return total_units / total_count / INSTANCE_MEMORY_MB / 1000


def cost_lines(region: str, cron: str, function_id: str | None,
               num_secrets: int) -> list[str]:
    print("Fetching list prices from the Azure Retail Prices API...")
    try:
        runs = config.runs_per_month(cron)
        seconds, basis = run_basis(average_run_seconds(function_id) if function_id else None)
        gb_second, _ = retail_price(region, "Functions",
                                    "On Demand Execution Time", "On Demand")
        per_ten, _ = retail_price(region, "Functions",
                                  "On Demand Total Executions", "On Demand")
        gib = INSTANCE_MEMORY_MB / 1024
        compute = runs * seconds * gib * gb_second + runs / 10 * per_ten
        items = [(f"Functions (Flex Consumption): ~{runs:.0f} runs x {basis} x {gib:g} GiB",
                  compute)]
        if num_secrets:
            items.append(key_vault_item(region, runs))
    except Exception as exc:
        fail(f"could not calculate the required monthly cost estimate: {exc}")
    return cost_estimate_lines(
        region, items, "excludes the shared Storage account, usually under $1/month")


def deploy(app: dict, assume_yes: bool) -> int:
    settings = preflight(app, azure_settings(app))
    name = app["name"]
    function_app = function_app_name(settings, name)
    cron = config.cron_expression(app["schedule"])
    values = gather_secrets(app)
    payload = json.dumps(values, sort_keys=True)
    digest = hashlib.sha256(payload.encode()).hexdigest()
    sid = secret_name(name)
    rg = settings["resource_group"]

    print(f"Checking current state in Azure subscription {settings['subscription']} "
          f"({settings['region']})...")
    group = az_json("group", "show", "--name", rg)
    require_managed(group, f"resource group {rg}")
    group_exists = group is not None
    storage = az_json("storage", "account", "show", "--name", settings["storage"],
                      "--resource-group", rg)
    require_managed(storage, f"Storage account {settings['storage']}")
    storage_exists = storage is not None
    workspace = workspace_resource(settings)
    require_managed(workspace, f"Log Analytics workspace {settings['workspace']}")
    logs_exist = workspace is not None
    current = az_json("functionapp", "show", "--name", function_app,
                      "--resource-group", rg)
    if current and not owned_by(current, name):
        fail(f"Function App {function_app} already exists but is not owned by "
             f"PDT app {name}; choose another resource group")
    vault_exists, current_hash = secret_state(settings, sid, name, values)

    actions = ["register required Azure resource providers"]
    actions.append(("use existing" if group_exists else "create")
                   + f" resource group {rg}")
    actions.append(("use existing" if storage_exists else "create")
                   + f" Storage account {settings['storage']} (Standard_LRS, shared)")
    actions.append(("use existing" if logs_exist else "create")
                   + f" Log Analytics workspace {settings['workspace']} (shared)")
    actions.append(("use existing" if current else "create")
                   + f" Application Insights {function_app} (run logs)")
    actions.append(("use existing" if vault_exists else "create")
                   + f" Key Vault {settings['vault']} (RBAC)")
    actions.append("ensure scoped Key Vault secret permissions for the deployer "
                   "and the Function App identity")
    actions.append("build a Docker-free zip on this computer; Azure installs the packages")
    actions += secret_actions(sid, values, current_hash, digest)
    actions.append(("update" if current else "create")
                   + f' Function App {function_app}: "{cron}" (UTC, Flex Consumption, '
                   f'{INSTANCE_MEMORY_MB} MB, 30-minute limit)')
    if not confirm(actions, assume_yes, cost_lines(
            settings["region"], cron, current["id"] if current else None, 1 if values else 0)):
        print("Aborted; nothing was changed.")
        return 1

    vault_id = ensure_group_and_vault(settings, PROVIDERS, vault_exists)
    if not storage_exists:
        print(f"==> creating Storage account {settings['storage']}")
        run_quiet("storage", "account", "create", "--name", settings["storage"],
                  "--resource-group", rg, "--location", settings["region"],
                  "--sku", "Standard_LRS", "--allow-blob-public-access", "false",
                  "--tags", "managed-by=pdt")
    print("==> building zip")
    archive = build_package(app)
    try:
        secret_uri = ensure_secret(settings, sid, values, payload, digest, current_hash, name)
        if not current:
            print(f"==> creating Function App {function_app}")
            run_quiet("functionapp", "create", "--name", function_app,
                      "--resource-group", rg, "--storage-account", settings["storage"],
                      "--flexconsumption-location", settings["region"],
                      "--runtime", "python", "--runtime-version", PYTHON_VERSION,
                      "--instance-memory", str(INSTANCE_MEMORY_MB),
                      "--workspace", settings["workspace"],
                      "--tags", "managed-by=pdt", f"pdt-app={name}")
            # az created the App Insights component; tag it so destroy owns it.
            run_quiet("resource", "tag", "--resource-group", rg,
                      "--name", function_app,
                      "--resource-type", "microsoft.insights/components",
                      "--tags", "managed-by=pdt", f"pdt-app={name}")
        identity = az_json("functionapp", "identity", "assign", "--name", function_app,
                           "--resource-group", rg)
        if not identity:
            fail(f"could not assign an identity to Function App {function_app}")
        assign_role(vault_id, identity["principalId"], "Key Vault Secrets User")
        app_settings = [f"PDT_SCHEDULE={ncrontab(cron)}"]
        if secret_uri:
            app_settings.append(f"PDT_ENV_JSON=@Microsoft.KeyVault(SecretUri={secret_uri})")
        print(f"==> configuring Function App {function_app}")
        run_quiet("functionapp", "config", "appsettings", "set", "--name", function_app,
                  "--resource-group", rg, "--settings", *app_settings)
        if not secret_uri:
            run_quiet("functionapp", "config", "appsettings", "delete", "--name",
                      function_app, "--resource-group", rg, "--setting-names", "PDT_ENV_JSON")
        print(f"==> uploading code to {function_app} (Azure builds the packages)")
        run_stream("functionapp", "deployment", "source", "config-zip", "--name",
                   function_app, "--resource-group", rg, "--src", str(archive),
                   "--build-remote", "true")
    finally:
        shutil.rmtree(archive.parent, ignore_errors=True)
    print(f"Deployed {name}.")
    print(f"Logs: https://portal.azure.com/#resource{current['id'] if current else ''}")
    return 0


def destroy(app: dict, assume_yes: bool) -> int:
    settings = preflight(app, azure_settings(app))
    name = app["name"]
    function_app = function_app_name(settings, name)
    sid = secret_name(name)
    rg = settings["resource_group"]
    current = az_json("functionapp", "show", "--name", function_app, "--resource-group", rg)
    managed_app = owned_by(current, name)
    insights = az_json("resource", "show", "--resource-group", rg, "--name", function_app,
                       "--resource-type", "microsoft.insights/components")
    managed_insights = owned_by(insights, name)
    secret_owned = managed_secret(settings, sid, name)
    if current and not managed_app:
        print(f"note: Function App {function_app} is not owned by this app; keeping it")
    others = other_pdt_apps(rg, name)
    if group_can_be_deleted(settings, others):
        actions = [
            f"delete resource group {rg} and everything in it: the Function App, "
            "its Application Insights, and the shared Storage account, Key Vault, "
            "and Log Analytics workspace",
            f"purge the soft-deleted Key Vault {settings['vault']}",
        ]
        if not confirm(actions, assume_yes):
            print("Aborted; nothing was changed.")
            return 1
        destroy_group(settings)
        return 0
    actions = []
    if managed_app:
        actions.append(f"delete Function App {function_app}")
    if managed_insights:
        actions.append(f"delete Application Insights {function_app}")
    if secret_owned:
        actions.append(f"delete and purge Key Vault secret {sid}")
    if not actions:
        print(f"Nothing owned by {name} to remove in resource group {rg}.")
        return 0
    if not confirm(actions, assume_yes):
        print("Aborted; nothing was changed.")
        return 1
    if managed_app:
        run_quiet("functionapp", "delete", "--name", function_app, "--resource-group", rg)
    if managed_insights:
        run_quiet("resource", "delete", "--resource-group", rg, "--name", function_app,
                  "--resource-type", "microsoft.insights/components")
    if secret_owned:
        purge_secret(settings, sid)
    report_shared_kept(rg, others)
    return 0
