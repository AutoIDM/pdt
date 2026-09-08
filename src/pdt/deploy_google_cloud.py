#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "pyyaml",
#     "rich",
#     "python-dotenv",
#     "backoff",
#     "gcsfs",
#     "fsspec",
#     "google-cloud-storage",
#     "google-auth",
#     "duckdb",
# ]
# ///
"""Deploy an app to Google Cloud as a scheduled Cloud Run job.

Resources per app, created in the configured project/region:
  Cloud Run job          pdt-<app>      (labeled managed-by=pdt)
  Cloud Scheduler job    pdt-<app>      (triggers the Run job)
  Secret Manager secret  pdt-<app>-env  (all env vars as one json blob)
Shared across apps:
  Artifact Registry repo PDT_ARTIFACT_REGISTRY_REPO env var, default "pdt"
  Service account        PDT_CLOUD_RUN_SERVICE_ACCOUNT env var, default
                         pdt-runner@<project> (created if missing)
  Storage bucket         pdt-data-<suffix> (kept after destroy; one folder
                         per app, the runner may write only its own)

If gcloud is not installed, pdt/gcloud_sdk.py offers to download a
pinned copy to the pdt data folder and every call here uses that copy.

Deploy reconciles: it creates what is missing and updates what changed,
so it is safe to re-run after a failure. Secrets and the image build
context are prepared by pdt/deploy_common.py.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pdt import config
from pdt import console
from pdt import gcloud_sdk
from pdt import terraform
from pdt import terraform_google
from pdt.deploy import confirm
from pdt.deploy_common import (
    STORE_TAGS, CostEstimate, fail, fetch_json, gather_secrets, image_action,
    stage_build_context, store_cost_label, store_kept_line, store_name, store_plan_lines,
    warn_if_locked, write_dockerfile)
from pdt import storage_cli
from pdt.utils import email_auth
from pdt.utils.storage import Store

GCLOUD = "gcloud"

APIS = (
    "artifactregistry.googleapis.com",
    "cloudbilling.googleapis.com",
    "cloudbuild.googleapis.com",
    "cloudscheduler.googleapis.com",
    "iam.googleapis.com",
    "run.googleapis.com",
    "secretmanager.googleapis.com",
    "storage.googleapis.com",
)
DESTROY_APIS = (
    "artifactregistry.googleapis.com",
    "cloudscheduler.googleapis.com",
    "iam.googleapis.com",
    "run.googleapis.com",
    "secretmanager.googleapis.com",
    "storage.googleapis.com",
)
STORE_ROLE = "roles/storage.objectUser"

BILLING_API = "https://cloudbilling.googleapis.com/v1"
# Cloud Run job defaults; the deploy below does not override them.
JOB_VCPU = 1.0
JOB_MEMORY_GIB = 0.5
ASSUMED_RUN_MINUTES = 5.0
RECENT_RUNS = 3


def run_quiet(*args: str, data: str | None = None) -> str:
    # a freshly enabled API can report SERVICE_DISABLED for a few minutes
    for wait in (10, 20, 40, 60, 60, 0):
        proc = subprocess.run([GCLOUD, *args], input=data if data is not None else "",
                              capture_output=True, text=True)
        if proc.returncode == 0:
            return proc.stdout
        if wait == 0 or "SERVICE_DISABLED" not in proc.stderr:
            break
        console.bullet(f"API not ready yet; retrying in {wait}s...", indent=4)
        time.sleep(wait)
    console.say(proc.stderr.strip())
    fail(f"pdt gcloud {' '.join(args[:4])} failed; fix the problem above and re-run the deploy")


def run_stream(*args: str) -> None:
    proc = subprocess.run([GCLOUD, *args])
    if proc.returncode != 0:
        fail(f"pdt gcloud {' '.join(args[:2])} failed; fix the problem above and re-run the deploy")


def describe_json(*args: str):
    proc = subprocess.run([GCLOUD, *args, "--format=json"], stdin=subprocess.DEVNULL,
                          capture_output=True, text=True)
    if proc.returncode != 0:
        return None
    return json.loads(proc.stdout or "null")


def read_json_or_none(*args: str):
    proc = subprocess.run([GCLOUD, *args, "--format=json"], stdin=subprocess.DEVNULL,
                          capture_output=True, text=True)
    if proc.returncode == 0:
        return json.loads(proc.stdout or "null")
    detail = proc.stderr.strip()
    lowered = detail.lower()
    if "not found" in lowered or "not_found" in lowered:
        return None
    if detail:
        console.say(detail)
    fail(f"pdt gcloud {' '.join(args[:4])} failed while checking resource ownership")


def list_json(*args: str) -> list:
    return json.loads(run_quiet(*args, "--format=json") or "[]")


def managed_by_pdt(resource: dict | None) -> bool:
    if resource is None:
        return False
    labels = resource.get("labels") or (resource.get("metadata") or {}).get("labels") or {}
    return labels.get("managed-by") == "pdt"


def require_managed(resource: dict | None, label: str) -> None:
    if resource is not None and not managed_by_pdt(resource):
        fail(f"{label} exists but is not managed by PDT")


def can_ask() -> bool:
    return email_auth.can_prompt(None)


def have_credentials() -> bool:
    proc = subprocess.run([GCLOUD, "auth", "print-access-token"],
                          stdin=subprocess.DEVNULL, capture_output=True, text=True)
    return proc.returncode == 0 and proc.stdout.strip() != ""


def login_with_key_file() -> bool:
    key_file = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
    if key_file == "" or not Path(key_file).is_file():
        return False
    console.status(f"Signing in to Google Cloud with {key_file}...")
    subprocess.run([GCLOUD, "--quiet", "auth", "login", "--cred-file", key_file],
                   stdin=subprocess.DEVNULL, capture_output=True, text=True)
    return have_credentials()


def ensure_credentials() -> None:
    if have_credentials():
        return
    if login_with_key_file():
        return
    if not can_ask():
        fail("no Google Cloud sign-in on this computer; set GOOGLE_APPLICATION_CREDENTIALS "
             f"to a service account key file, or run {GCLOUD} auth login")
    console.warn("gcloud has no active Google account yet.")
    try:
        answer = input("Log in now (opens a browser)? [y/N] ").strip().lower()
    except EOFError:
        answer = ""
    if answer not in ("y", "yes"):
        fail(f"log in first: {GCLOUD} auth login")
    login = subprocess.run([GCLOUD, "auth", "login"])
    if login.returncode != 0:
        fail("gcloud auth login failed")


def preflight(app: dict, project: str, assume_yes: bool) -> str:
    global GCLOUD
    try:
        GCLOUD = gcloud_sdk.ensure_gcloud(assume_yes)
    except gcloud_sdk.GcloudError as e:
        fail(str(e))
    ensure_credentials()
    if project in ("", "my-project"):
        project = choose_project(app, project)
    return project


def choose_project(app: dict, requested: str) -> str:
    if not can_ask():
        fail("no Google Cloud project to deploy to; set platform.project in pdt.yml, "
             "or set the PDT_GOOGLE_CLOUD_PROJECT environment variable")
    available = [line.split("\t") for line in
                 run_quiet("projects", "list", "--format=value(projectId,name)").splitlines()
                 if line.strip()]
    if not available:
        fail("your Google account has no project yet; create one at "
             "https://console.cloud.google.com/projectcreate")
    if requested:
        console.warn(f"platform.project {requested!r} is not a real Google Cloud project id.")
    console.heading("Your Google Cloud projects:")
    for index, entry in enumerate(available, 1):
        console.choice(index, entry[0], entry[-1])
    try:
        answer = input(f"Deploy to which one? [1-{len(available)}] ").strip()
    except EOFError:
        answer = ""
    if not answer.isdigit() or not 1 <= int(answer) <= len(available):
        fail("no Google Cloud project selected")
    project = available[int(answer) - 1][0]
    saved = config.save_platform_key(app, "project", project)
    console.done(f"Saved project {project} to {saved.relative_to(config.find_project())}.")
    return project


def relogin(assume_yes: bool) -> int:
    global GCLOUD
    try:
        GCLOUD = gcloud_sdk.ensure_gcloud(assume_yes)
    except gcloud_sdk.GcloudError as e:
        fail(str(e))
    console.status("Revoking the cached Google Cloud logins on this computer...")
    subprocess.run([GCLOUD, "auth", "revoke", "--all"], stdin=subprocess.DEVNULL,
                   capture_output=True, text=True)
    if subprocess.run([GCLOUD, "auth", "login"]).returncode != 0:
        fail("pdt gcloud auth login failed")
    account = subprocess.run(
        [GCLOUD, "auth", "list", "--filter=status:ACTIVE", "--format=value(account)"],
        stdin=subprocess.DEVNULL, capture_output=True, text=True).stdout.strip()
    console.done(f"Signed in as {account}")
    return 0


def project_region(app: dict) -> tuple[str, str]:
    project = str(app["platform"].get("project")
                  or os.environ.get("PDT_GOOGLE_CLOUD_PROJECT") or "")
    region = str(app["platform"].get("region")
                 or os.environ.get("PDT_GOOGLE_CLOUD_REGION") or "us-central1")
    return project, region


def secret_id(app_name: str) -> str:
    return f"pdt-{app_name}-env"


def enabled_services(project: str) -> set[str]:
    return set(run_quiet("services", "list", "--enabled", "--project", project,
                         "--format=value(config.name)").splitlines())


def store_bucket(project: str) -> str:
    return store_name(project)


def store_url(bucket: str, app_name: str) -> str:
    return f"gs://{bucket}/{app_name}/"


def store_condition(bucket: str, app_name: str) -> str:
    expression = f'resource.name.startsWith("projects/_/buckets/{bucket}/objects/{app_name}/")'
    return f"expression={expression},title=pdt-{app_name}"


def deployer_store(project: str, app_name: str) -> Store:
    from google.oauth2.credentials import Credentials

    token = run_quiet("auth", "print-access-token").strip()
    return Store(store_url(store_bucket(project), app_name), Credentials(token))


def store_grant_exists(bucket: str, app_name: str, sa: str) -> bool:
    policy = describe_json("storage", "buckets", "get-iam-policy", f"gs://{bucket}") or {}
    for binding in policy.get("bindings") or []:
        condition = binding.get("condition") or {}
        if (binding.get("role") == STORE_ROLE
                and condition.get("title") == f"pdt-{app_name}"
                and f"serviceAccount:{sa}" in (binding.get("members") or [])):
            return True
    return False


def scheduler_description(app_name: str) -> str:
    return f"Managed by PDT app {app_name}"


def job_logs_url(project: str, region: str, job: str) -> str:
    # The Logs tab on the Cloud Run job page: every execution's output, newest first.
    return (f"https://console.cloud.google.com/run/jobs/details/{region}/{job}"
            f"/logs?project={project}")


def scheduler_owned(resource: dict | None, app_name: str, project: str,
                    region: str, service_account: str) -> bool:
    if resource is None:
        return False
    if resource.get("description") == scheduler_description(app_name):
        return True
    target = resource.get("httpTarget") or {}
    token = target.get("oauthToken") or {}
    expected_uri = (f"https://{region}-run.googleapis.com/apis/run.googleapis.com"
                    f"/v1/namespaces/{project}/jobs/pdt-{app_name}:run")
    return (resource.get("description") in (None, "")
            and target.get("uri") == expected_uri
            and token.get("serviceAccountEmail") == service_account)


def service_account_owned(resource: dict | None) -> bool:
    return resource is not None and resource.get("displayName") == "pdt job runner"


def image_name(image: dict) -> str:
    value = str(image.get("package") or image.get("name") or "").rstrip("/")
    return value.rsplit("/", 1)[-1].split("@", 1)[0]


def run_job_identity(run_job: dict) -> tuple[str, str]:
    metadata = run_job.get("metadata") or {}
    value = str(run_job.get("name") or metadata.get("name") or "")
    labels = run_job.get("labels") or metadata.get("labels") or {}
    region = str(labels.get("cloud.googleapis.com/location") or "")
    parts = value.split("/")
    if "locations" in parts:
        index = parts.index("locations")
        if index + 1 < len(parts):
            region = parts[index + 1]
    return value.rstrip("/").rsplit("/", 1)[-1], region


def needs_oauth_cache_updates(values: dict) -> bool:
    if values.get("PDT_GRAPH_MAIL_CACHE_B64", "") != "":
        return True
    host = values.get("PDT_SMTP_HOST", "").lower().rstrip(".")
    return (
        host in ("smtp.office365.com", "smtp-mail.outlook.com")
        and values.get("PDT_SMTP_OAUTH_CACHE_B64", "") != ""
    )


def secret_value(project: str, sid: str) -> str | None:
    proc = subprocess.run(
        [GCLOUD, "secrets", "versions", "access", "latest",
         "--secret", sid, "--project", project],
        stdin=subprocess.DEVNULL, capture_output=True, text=True)
    if proc.returncode != 0:
        return None
    return proc.stdout


def build_image(app: dict, image: str, project: str) -> None:
    stage = stage_build_context(app)
    try:
        write_dockerfile(stage, app)
        run_stream("builds", "submit", str(stage), "--tag", image, "--project", project)
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def billing_list(path: str, key: str, project: str) -> list:
    token = run_quiet("auth", "print-access-token").strip()
    items = []
    page_token = ""
    while True:
        query = {"pageSize": "5000"}
        if page_token:
            query["pageToken"] = page_token
        req = urllib.request.Request(
            f"{BILLING_API}/{path}?{urllib.parse.urlencode(query)}",
            headers={"Authorization": f"Bearer {token}",
                     "X-Goog-User-Project": project})
        data = fetch_json(req, timeout=60)
        items.extend(data.get(key) or [])
        page_token = data.get("nextPageToken") or ""
        if not page_token:
            return items


def sku_price(skus: list, region: str, description: str,
              prefer: str = "") -> tuple[float, str]:
    matches = []
    for sku in skus:
        if (sku.get("category") or {}).get("usageType") != "OnDemand":
            continue
        regions = sku.get("serviceRegions") or []
        if region not in regions and "global" not in regions:
            continue
        # startswith, not substring: "Jobs CPU" must not match "Delayed Jobs CPU"
        if sku.get("description", "").lower().startswith(description.lower()):
            matches.append(sku)
    if not matches:
        raise LookupError(f"no {description!r} SKU priced for region {region}")
    preferred = [s for s in matches
                 if prefer and prefer.lower() in s["description"].lower()]
    sku = (preferred or matches)[0]
    expr = sku["pricingInfo"][0]["pricingExpression"]
    rate = expr["tieredRates"][-1]["unitPrice"]
    price = int(rate.get("units") or 0) + int(rate.get("nanos") or 0) / 1e9
    return price, expr.get("usageUnit", "")


def per_month(usage_unit: str) -> float:
    # "count" is the Cloud Scheduler job-day SKU, priced at 1/31 of the monthly rate
    factors = {"mo": 1.0, "d": 30.44, "h": 730.0, "count": 31.0}
    if usage_unit not in factors:
        raise LookupError(f"unexpected pricing unit {usage_unit!r}")
    return factors[usage_unit]


def average_run_seconds(job: str, region: str, project: str) -> float | None:
    execs = describe_json("run", "jobs", "executions", "list", "--job", job,
                          "--region", region, "--project", project,
                          "--limit", str(RECENT_RUNS)) or []
    durations = []
    for execution in execs:
        status = execution.get("status") or {}
        start = status.get("startTime")
        end = status.get("completionTime")
        if start and end:
            begun = datetime.datetime.fromisoformat(start)
            done = datetime.datetime.fromisoformat(end)
            durations.append((done - begun).total_seconds())
    if not durations:
        return None
    return sum(durations) / len(durations)


def billing_detail(exc: Exception) -> str:
    # fetch_json wraps an HTTP error response in an API error; the body
    # still names the reason ("Cloud Billing API has not been used...").
    response = getattr(exc, "response", None) or exc
    if isinstance(response, urllib.error.HTTPError):
        try:
            return json.loads(response.read()).get("error", {}).get("message", "")
        except (ValueError, OSError):
            return ""
    return ""


def cost_estimate(project: str, region: str, cron: str, job: str,
                        job_exists: bool, num_secrets: int,
                        assume_yes: bool, billing_confirmed: bool = False,
                        attempt: int = 0, store_usage: tuple[int, int] | None = None) -> CostEstimate:
    console.status("Fetching list prices from the Cloud Billing catalog...")
    try:
        runs = config.runs_per_month(cron)
        seconds = average_run_seconds(job, region, project) if job_exists else None
        if seconds is None:
            seconds = ASSUMED_RUN_MINUTES * 60
            basis = f"{ASSUMED_RUN_MINUTES:g} min assumed"
        else:
            basis = f"{seconds / 60:.1f} min avg of recent runs"
        services = billing_list("services", "services", project)
        ids = {s.get("displayName"): s.get("serviceId") for s in services}
        run_skus = billing_list(f"services/{ids['Cloud Run']}/skus", "skus", project)
        sched_skus = billing_list(f"services/{ids['Cloud Scheduler']}/skus", "skus", project)
        secret_skus = billing_list(f"services/{ids['Secret Manager']}/skus", "skus", project)
        cpu_price, _ = sku_price(run_skus, region, "Jobs CPU")
        mem_price, _ = sku_price(run_skus, region, "Jobs Memory")
        run_cost = runs * seconds * (JOB_VCPU * cpu_price + JOB_MEMORY_GIB * mem_price)
        sched_price, sched_unit = sku_price(sched_skus, region, "Job")
        sched_cost = sched_price * per_month(sched_unit)
        items = [
            (f"Cloud Run job: ~{runs:.0f} runs x {basis} "
             f"x {JOB_VCPU:g} vCPU / {JOB_MEMORY_GIB:g} GiB", run_cost),
            ("Cloud Scheduler job", sched_cost),
        ]
        if num_secrets:
            secret_price, secret_unit = sku_price(secret_skus, region,
                                                  "Secret version", prefer="storage")
            secret_cost = num_secrets * secret_price * per_month(secret_unit)
            items.append((f"Secret Manager: {num_secrets} secret version", secret_cost))
        if store_usage is not None:
            storage_skus = billing_list(f"services/{ids['Cloud Storage']}/skus", "skus", project)
            storage_price, _ = sku_price(storage_skus, region, "Standard Storage")
            count, size = store_usage
            items.append((store_cost_label(count, size), size / 1024 ** 3 * storage_price))
    except Exception as exc:
        detail = billing_detail(exc)
        disabled = "has not been used" in detail or "SERVICE_DISABLED" in detail
        if disabled and not billing_confirmed:
            actions = ["prepare protected Terraform state for the required cost estimate",
                       f"enable the Cloud Billing API in project {project} to calculate the estimate"]
            if not confirm(actions, assume_yes):
                console.warn("Aborted; nothing was changed.")
                raise SystemExit(1)
            console.step("enabling the Cloud Billing API")
            with deployment_context({"name": job.removeprefix("pdt-")}, project, region, True) as deployment:
                resources = {"google_project_service": {"billing": {
                    "project": project, "service": "cloudbilling.googleapis.com", "disable_on_destroy": False,
                }}}
                workspace = deployment.workspace(
                    "global", terraform.configuration("google", provider_settings(project, region), resources), retain=True)
                workspace.apply(workspace.plan())
            billing_confirmed = True
        waits = (10, 20, 40, 60)
        if disabled and attempt < len(waits):
            wait = waits[attempt]
            console.bullet(f"Cloud Billing API is not ready; retrying in {wait}s...", indent=4)
            time.sleep(wait)
            return cost_estimate(project, region, cron, job,
                                 job_exists, num_secrets, assume_yes,
                                 billing_confirmed,
                                 attempt + 1, store_usage)
        fail(f"could not calculate the required monthly cost estimate: "
             f"{detail or str(exc)}")
    return CostEstimate(items, f"{region} list prices, before free tiers",
                        "excludes Cloud Build image builds and Artifact Registry storage")


def imports_for_deploy(app_name: str, project: str, region: str, repository: str,
                       service_account: str, has_secret: bool,
                       oauth_cache_updates: bool, require_account: bool = True) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    """Return imports after verifying that every collision belongs to PDT."""
    job = f"pdt-{app_name}"
    global_imports = {}
    shared_imports = {}
    app_imports = {}
    enabled = set(run_quiet("services", "list", "--enabled", "--project", project,
                            "--format=value(config.name)").splitlines())
    for service in terraform_google.api_resources(project)["google_project_service"]["pdt"]["for_each"]:
        if service in enabled:
            global_imports[f'google_project_service.pdt["{service}"]'] = f"{project}/{service}"
    default_account = f"pdt-runner@{project}.iam.gserviceaccount.com"
    if "iam.googleapis.com" in enabled:
        account = read_json_or_none("iam", "service-accounts", "describe", service_account,
                                    "--project", project)
        if service_account == default_account:
            if account is not None:
                if not service_account_owned(account):
                    fail(f"service account {service_account} exists but is not managed by PDT")
                global_imports["google_service_account.runner"] = (
                    f"projects/{project}/serviceAccounts/{service_account}")
        elif account is None and require_account:
            fail(f"service account {service_account} does not exist in project {project}")
    if "artifactregistry.googleapis.com" in enabled:
        repository_state = read_json_or_none("artifacts", "repositories", "describe", repository,
                                             "--location", region, "--project", project)
        if repository_state is not None:
            require_managed(repository_state, f"Artifact Registry repository {repository}")
            shared_imports["google_artifact_registry_repository.images"] = (
                f"projects/{project}/locations/{region}/repositories/{repository}")
    member = f"serviceAccount:{service_account}"
    if "run.googleapis.com" in enabled:
        job_state = read_json_or_none("run", "jobs", "describe", job, "--region", region,
                                      "--project", project)
        if job_state is not None:
            require_managed(job_state, f"Cloud Run job {job}")
            job_id = f"projects/{project}/locations/{region}/jobs/{job}"
            app_imports["google_cloud_run_v2_job.job"] = job_id
            policy = read_json_or_none("run", "jobs", "get-iam-policy", job,
                                       "--region", region, "--project", project) or {}
            if any(binding.get("role") == "roles/run.invoker" and member in binding.get("members", [])
                   and not binding.get("condition") for binding in policy.get("bindings", [])):
                app_imports["google_cloud_run_v2_job_iam_member.invoker"] = (
                    f"{job_id} roles/run.invoker {member}")
    if "cloudscheduler.googleapis.com" in enabled:
        schedule = read_json_or_none("scheduler", "jobs", "describe", job, "--location", region,
                                     "--project", project)
        if schedule is not None:
            if not scheduler_owned(schedule, app_name, project, region, service_account):
                fail(f"Cloud Scheduler job {job} exists but is not managed by PDT")
            app_imports["google_cloud_scheduler_job.schedule"] = (
                f"projects/{project}/locations/{region}/jobs/{job}")
    if has_secret and "secretmanager.googleapis.com" in enabled:
        secret = secret_id(app_name)
        secret_state = read_json_or_none("secrets", "describe", secret, "--project", project)
        if secret_state is not None:
            require_managed(secret_state, f"Secret Manager secret {secret}")
            sid = f"projects/{project}/secrets/{secret}"
            app_imports["google_secret_manager_secret.env"] = sid
            roles = ["roles/secretmanager.secretAccessor"]
            if oauth_cache_updates:
                roles.append("roles/secretmanager.secretVersionAdder")
            policy = read_json_or_none("secrets", "get-iam-policy", secret, "--project", project) or {}
            for index, role in enumerate(roles):
                if any(binding.get("role") == role and member in binding.get("members", [])
                       and not binding.get("condition") for binding in policy.get("bindings", [])):
                    app_imports[f"google_secret_manager_secret_iam_member.runner_{index}"] = (
                        f"{sid} {role} {member}")
    return global_imports, shared_imports, app_imports


def provider_settings(project: str, region: str) -> dict[str, str]:
    return {"project": project, "region": region}


def deployment_context(app: dict, project: str, region: str, assume_yes: bool):
    token = run_quiet("auth", "print-access-token").strip()
    return terraform.Deployment(
        app, provider="google-cloud", identity={"project": project, "region": region,
                                                  "runtime": "cloud-run-job"},
        env={"GOOGLE_OAUTH_ACCESS_TOKEN": token}, assume_yes=assume_yes)


def deploy(app: dict, assume_yes: bool) -> int:
    name = app["name"]
    project, region = project_region(app)
    project = preflight(app, project, assume_yes)
    cron = config.cron_expression(app["schedule"])
    values = gather_secrets(app)
    job = f"pdt-{name}"
    repository = os.environ.get("PDT_ARTIFACT_REGISTRY_REPO", "").strip() or "pdt"
    service_account = os.environ.get("PDT_CLOUD_RUN_SERVICE_ACCOUNT", "").strip() or (
        f"pdt-runner@{project}.iam.gserviceaccount.com")
    oauth_cache_updates = needs_oauth_cache_updates(values)
    global_imports, shared_imports, app_imports = imports_for_deploy(
        name, project, region, repository, service_account, bool(values), oauth_cache_updates)
    bucket = store_bucket(project)
    store = deployer_store(project, name) if app["storage"] else None
    bucket_exists = False
    # Terraform enables the storage API below, so a first deploy cannot read the bucket yet.
    if store and "storage.googleapis.com" in enabled_services(project):
        described = read_json_or_none("storage", "buckets", "describe", f"gs://{bucket}")
        require_managed(described, f"bucket {bucket}")
        bucket_exists = described is not None
    usage = (store.usage() if bucket_exists else (0, 0)) if store else None
    billing_confirmed = False
    cost = cost_estimate(project, region, cron, job, bool(
        terraform_google.address("cloud_run_v2_job", "job") in app_imports),
        1 if values else 0, assume_yes, billing_confirmed, store_usage=usage)
    actions = ["set up protected Terraform state",
               f"reconcile Google Cloud infrastructure for {name}",
               image_action(app, f"build and push image for {name}")]
    if values:
        actions.append(f"write the current env values to secret {secret_id(name)}")
    if store:
        actions += store_plan_lines(f"bucket {bucket}", bucket_exists, service_account, name)
    if not confirm(actions, assume_yes, cost):
        console.warn("Aborted; nothing was changed.")
        return 1
    settings = provider_settings(project, region)
    with deployment_context(app, project, region, assume_yes) as deployment:
        deployment.save()
        api_imports = {key: value for key, value in global_imports.items()
                       if key.startswith("google_project_service.")}
        global_workspace = deployment.workspace(
            "global", terraform.configuration("google", settings, terraform_google.api_resources(project)),
            imports=api_imports, retain=True)
        global_workspace.apply(global_workspace.plan())
        global_imports, shared_imports, app_imports = imports_for_deploy(
            name, project, region, repository, service_account, bool(values), oauth_cache_updates)
        global_workspace = deployment.workspace(
            "global", terraform.configuration("google", settings,
                                               terraform_google.global_resources(project, service_account)),
            imports=global_imports, retain=True)
        global_workspace.apply(global_workspace.plan())
        shared_workspace = deployment.workspace(
            "shared", terraform.configuration("google", settings,
                                               terraform_google.shared_resources(project, region, repository)),
            imports=shared_imports, retain=True)
        shared_workspace.apply(shared_workspace.plan())
        if store:
            # The bucket is kept after destroy, so it stays outside Terraform's state.
            if not bucket_exists:
                console.step(f"creating bucket {bucket}")
                run_quiet("storage", "buckets", "create", f"gs://{bucket}",
                          "--location", region, "--project", project,
                          "--uniform-bucket-level-access", "--public-access-prevention")
                run_quiet("storage", "buckets", "update", f"gs://{bucket}", "--update-labels",
                          ",".join(f"{key}={value}" for key, value in STORE_TAGS.items()))
            console.step(f"granting {service_account} write access to {bucket}/{name}/")
            run_quiet("storage", "buckets", "add-iam-policy-binding", f"gs://{bucket}",
                      "--member", f"serviceAccount:{service_account}", "--role", STORE_ROLE,
                      "--condition", store_condition(bucket, name))
        image_tag = f"{region}-docker.pkg.dev/{project}/{repository}/{name}:{int(time.time())}"
        console.step(f"building image {image_tag}")
        build_image(app, image_tag, project)
        digest = run_quiet("artifacts", "docker", "images", "describe", image_tag,
                           "--project", project, "--format=value(image_summary.digest)").strip()
        if not digest.startswith("sha256:"):
            fail(f"Artifact Registry did not return a digest for {image_tag}")
        image = f"{image_tag.rsplit(':', 1)[0]}@{digest}"
        data = None
        account_reference = service_account
        if service_account != f"pdt-runner@{project}.iam.gserviceaccount.com":
            data = {"google_service_account": {"runner": {
                "project": project, "account_id": service_account,
            }}}
            account_reference = "${data.google_service_account.runner.email}"
        resources = terraform_google.app_resources(
            name, project, region, cron, app["timezone"], image, account_reference,
            secret_id(name), bool(values), oauth_cache_updates,
            store.url if store else None)
        if values:
            prerequisites = {key: value for key, value in resources.items()
                             if key in ("google_secret_manager_secret", "google_secret_manager_secret_iam_member")}
            prerequisite_imports = {key: value for key, value in app_imports.items()
                                    if key.startswith(("google_secret_manager_secret.", "google_secret_manager_secret_iam_member."))}
            app_workspace = deployment.workspace(
                "app", terraform.configuration("google", settings, prerequisites, data=data),
                imports=prerequisite_imports, retain=True)
            app_workspace.apply(app_workspace.plan())
            run_quiet("secrets", "versions", "add", secret_id(name), "--project", project,
                      "--data-file", "-", data=json.dumps(values, sort_keys=True))
        app_workspace = deployment.workspace(
            "app", terraform.configuration("google", settings, resources, data=data), imports=app_imports)
        app_workspace.apply(app_workspace.plan())
        deployment.save()
    console.done(f"Deployed {name}.")
    console.field("Run it once", f"pdt gcloud run jobs execute {job} --region {region} --project {project}")
    console.field("Run logs", job_logs_url(project, region, job))
    return 0




def destroy(app: dict, assume_yes: bool) -> int:
    """Adopt owned resources, then remove the app and unused shared resources."""
    name = app["name"]
    project, region = project_region(app)
    project = preflight(app, project, assume_yes)
    job = f"pdt-{name}"
    repository = os.environ.get("PDT_ARTIFACT_REGISTRY_REPO", "").strip() or "pdt"
    service_account = os.environ.get("PDT_CLOUD_RUN_SERVICE_ACCOUNT", "").strip() or (
        f"pdt-runner@{project}.iam.gserviceaccount.com")
    enabled = set(run_quiet("services", "list", "--enabled", "--project", project,
                            "--format=value(config.name)").splitlines())
    current = None
    if "run.googleapis.com" in enabled:
        current = read_json_or_none("run", "jobs", "describe", job, "--region", region, "--project", project)
    require_managed(current, f"Cloud Run job {job}")
    template = current or {}
    while "containers" not in template:
        if isinstance(template.get("template"), dict):
            template = template["template"]
        elif isinstance(template.get("spec"), dict):
            template = template["spec"]
        else:
            break
    containers = template.get("containers") or []
    image = containers[0].get("image", "unused") if containers else "unused"
    service_account = template.get("serviceAccountName") or template.get("serviceAccount") or (
        template.get("service_account") or service_account)
    image_parts = image.split("/")
    if len(image_parts) >= 4 and image_parts[:2] == [f"{region}-docker.pkg.dev", project]:
        repository = image_parts[2]
    secret = None
    if "secretmanager.googleapis.com" in enabled:
        secret = read_json_or_none("secrets", "describe", secret_id(name), "--project", project)
    require_managed(secret, f"Secret Manager secret {secret_id(name)}")
    oauth_cache_updates = any(item.get("name") == "PDT_ENV_SECRET_RESOURCE"
                              for container in containers for item in container.get("env", []))
    global_imports, shared_imports, app_imports = imports_for_deploy(
        name, project, region, repository, service_account, secret is not None, oauth_cache_updates, require_account=False)
    bucket = store_bucket(project)
    store = deployer_store(project, name) if app["storage"] else None
    store_present = (store is not None and "storage.googleapis.com" in enabled
                     and read_json_or_none("storage", "buckets", "describe",
                                           f"gs://{bucket}") is not None)
    revoke_grant = store_present and store_grant_exists(bucket, name, service_account)
    actions = ["prepare protected Terraform state for the removal",
               f"adopt and delete owned Cloud Run job, schedule, IAM grants, and secret for {name}",
               f"delete app images from Artifact Registry repository {repository}",
               "delete the registry and PDT service account when no other app uses them"]
    if revoke_grant:
        actions.insert(0, f"remove {service_account} write access to {bucket}/{name}/")
        warn_if_locked(store, name)
    if not confirm(actions, assume_yes):
        console.warn("Aborted; nothing was changed.")
        return 1
    if revoke_grant:
        console.step(f"removing {service_account} write access to {bucket}/{name}/")
        run_quiet("storage", "buckets", "remove-iam-policy-binding", f"gs://{bucket}",
                  "--member", f"serviceAccount:{service_account}", "--role", STORE_ROLE,
                  "--condition", store_condition(bucket, name))
    settings = provider_settings(project, region)
    with deployment_context(app, project, region, assume_yes) as deployment:
        deployment.save()
        global_imports, shared_imports, app_imports = imports_for_deploy(
            name, project, region, repository, service_account, secret is not None, oauth_cache_updates, require_account=False)
        resources = terraform_google.app_resources(
            name, project, region, config.cron_expression(app["schedule"]), app["timezone"],
            image, service_account, secret_id(name), secret is not None, oauth_cache_updates)
        for kind, instances in resources.items():
            for resource_name in list(instances):
                if f"{kind}.{resource_name}" not in app_imports:
                    del instances[resource_name]
                    continue
                item = instances[resource_name]
                item.pop("depends_on", None)
                if kind == "google_cloud_run_v2_job":
                    item["lifecycle"] = {"ignore_changes": ["project", "location", "name", "labels", "template"]}
                else:
                    item["lifecycle"] = {"ignore_changes": "all"}
        workspace = deployment.workspace("app", terraform.configuration("google", settings, resources), imports=app_imports)
        if "google_cloud_run_v2_job.job" in app_imports:
            plan = workspace.plan()
            if any(action.startswith(("create ", "replace ")) for action in plan.actions):
                fail("the resources changed during removal; run the same destroy command again")
            workspace.apply(plan)
        workspace.apply(workspace.plan(destroy=True))
        regional_jobs = []
        project_jobs = []
        if "run.googleapis.com" in enabled:
            regional_jobs = list_json("run", "jobs", "list", "--region", region, "--project", project)
            project_jobs = list_json("run", "jobs", "list", "--project", project)
        other_regional_jobs = [item for item in regional_jobs if run_job_identity(item)[0] != job]
        other_project_jobs = [item for item in project_jobs
                              if run_job_identity(item) != (job, region)]
        other_images = []
        if shared_imports:
            images = list_json("artifacts", "docker", "images", "list",
                               f"{region}-docker.pkg.dev/{project}/{repository}",
                               "--include-tags", "--project", project)
            app_images = [item for item in images if image_name(item) == name]
            other_images = [item for item in images if image_name(item) != name]
            if app_images:
                run_quiet("artifacts", "docker", "images", "delete",
                          f"{region}-docker.pkg.dev/{project}/{repository}/{name}",
                          "--delete-tags", "--project", project, "--quiet")
        if not other_regional_jobs and not other_images and shared_imports:
            shared = deployment.existing_workspace("shared")
            if shared is None:
                shared = deployment.workspace(
                    "shared", terraform.configuration("google", settings,
                                                       terraform_google.shared_resources(project, region, repository)),
                    imports=shared_imports)
            shared.apply(shared.plan(destroy=True))
        if not other_project_jobs:
            global_workspace = deployment.existing_workspace("global")
            if global_workspace is None:
                resources = terraform_google.global_resources(project, service_account)
                if "google_service_account.runner" not in global_imports:
                    resources.pop("google_service_account", None)
                global_workspace = deployment.workspace(
                    "global", terraform.configuration("google", settings, resources), imports=global_imports)
            global_workspace.apply(global_workspace.plan(destroy=True))
        deployment.finish_destroy()
    console.done(f"Removed {name} from project {project}.")
    if other_regional_jobs or other_project_jobs or other_images:
        console.note("shared resources remain because other jobs or images still use them.")
    if store_present:
        console.say(store_kept_line(f"bucket {bucket}", store.usage()[0], name))
    return 0


def storage(app: dict, rest: list[str], assume_yes: bool) -> int:
    project, _ = project_region(app)
    project = preflight(app, project, assume_yes)
    return storage_cli.run(deployer_store(project, app["name"]), app, rest, assume_yes)


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "gcloud":
        try:
            binary = gcloud_sdk.ensure_gcloud()
        except gcloud_sdk.GcloudError as e:
            fail(str(e))
        return subprocess.run([binary, *sys.argv[2:]]).returncode
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("deploy", "destroy", "login", "storage"))
    parser.add_argument("app")
    parser.add_argument("rest", nargs="*")
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--profile", help="not used by Google Cloud")
    args = parser.parse_intermixed_args()
    try:
        app = config.merged_app(args.app)
    except config.ConfigError as exc:
        fail(str(exc))
    config.load_env(app["dir"])
    if args.command == "login":
        return relogin(args.yes)
    if args.command == "storage":
        return storage(app, args.rest, args.yes)
    try:
        if args.command == "deploy":
            return deploy(app, args.yes)
        return destroy(app, args.yes)
    except config.ConfigError as exc:
        fail(str(exc))


if __name__ == "__main__":
    sys.exit(main())
