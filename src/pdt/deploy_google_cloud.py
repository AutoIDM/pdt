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

If gcloud is not installed, pdt/gcloud_sdk.py downloads a
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
from pdt.deploy import confirm
from pdt.deploy_common import (
    STORE_TAGS, CostEstimate, fail, fetch_json, gather_secrets, image_action, run_secrets,
    stage_build_context, store_cost_label, store_kept_line, store_name, store_plan_lines,
    warn_if_locked, write_dockerfile)
from pdt import runs_cli
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
    "logging.googleapis.com",
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


# A freshly enabled API reports SERVICE_DISABLED for a few minutes, and Cloud
# Scheduler reports ABORTED when a job changed a moment ago. Both pass.
TRANSIENT = ("SERVICE_DISABLED", "ABORTED")


def run_quiet(*args: str, data: str | None = None) -> str:
    for wait in (10, 20, 40, 60, 60, 0):
        proc = subprocess.run([GCLOUD, *args], input=data if data is not None else "",
                              capture_output=True, text=True)
        if proc.returncode == 0:
            return proc.stdout
        if wait == 0 or not any(marker in proc.stderr for marker in TRANSIENT):
            break
        console.bullet(f"Google Cloud is not ready yet; retrying in {wait}s...", indent=4)
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


def not_found(detail: str) -> bool:
    # Cloud Run says "Cannot find job [x]"; the other services say "not found".
    lowered = detail.lower()
    return any(marker in lowered for marker in ("not found", "not_found", "cannot find"))


def read_json_or_none(*args: str):
    proc = subprocess.run([GCLOUD, *args, "--format=json"], stdin=subprocess.DEVNULL,
                          capture_output=True, text=True)
    if proc.returncode == 0:
        return json.loads(proc.stdout or "null")
    detail = proc.stderr.strip()
    if not_found(detail):
        return None
    if detail:
        console.say(detail)
    fail(f"pdt gcloud {' '.join(args[:4])} failed while checking resource ownership")


def list_json(*args: str) -> list:
    return json.loads(run_quiet(*args, "--format=json") or "[]")


def service_account_or_none(project: str, email: str) -> dict | None:
    # describe answers PERMISSION_DENIED, not NOT_FOUND, for an account that
    # does not exist, so a first deploy would stop here. list does not.
    accounts = list_json("iam", "service-accounts", "list", "--project", project)
    return next((account for account in accounts if account.get("email") == email), None)


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
        GCLOUD = gcloud_sdk.ensure_gcloud()
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
        answer = input(f"Deploy to which one? [1-{len(available)}, or a project id] ").strip()
    except EOFError:
        answer = ""
    ids = [entry[0] for entry in available]
    if answer.isdigit() and 1 <= int(answer) <= len(ids):
        project = ids[int(answer) - 1]
    elif answer in ids:
        project = answer
    else:
        fail("no Google Cloud project selected; type a number from the list or one of the project ids")
    saved = config.save_platform_key(app, "project", project)
    console.done(f"Saved project {project} to {saved.relative_to(config.find_project())}.")
    return project


def relogin(assume_yes: bool) -> int:
    global GCLOUD
    try:
        GCLOUD = gcloud_sdk.ensure_gcloud()
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


def ensure_apis(project: str, assume_yes: bool,
                required: tuple[str, ...] = APIS) -> bool:
    output = run_quiet(
        "services", "list", "--enabled", "--project", project,
        "--format=value(config.name)")
    enabled = set(output.splitlines())
    missing = [api for api in required if api not in enabled]
    if not missing:
        return False
    actions = [f"enable {api} in project {project}" for api in missing]
    if not confirm(actions, assume_yes):
        console.warn("Aborted; nothing was changed.")
        raise SystemExit(1)
    console.step("enabling required Google Cloud APIs")
    run_quiet("services", "enable", *missing, "--project", project)
    return "cloudbilling.googleapis.com" in missing


def project_region(app: dict) -> tuple[str, str]:
    project = str(app["platform"].get("project")
                  or os.environ.get("PDT_GOOGLE_CLOUD_PROJECT") or "")
    region = str(app["platform"].get("region")
                 or os.environ.get("PDT_GOOGLE_CLOUD_REGION") or "us-central1")
    return project, region


def secret_id(app_name: str) -> str:
    return f"pdt-{app_name}-env"


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
    return value.rsplit("/", 1)[-1]


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
            actions = [f"enable the Cloud Billing API in project {project} "
                       "to calculate the required cost estimate"]
            if not confirm(actions, assume_yes):
                console.warn("Aborted; nothing was changed.")
                raise SystemExit(1)
            console.step("enabling the Cloud Billing API")
            run_quiet("services", "enable", "cloudbilling.googleapis.com",
                      "--project", project)
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


def destroy_old_secret_versions(project: str, sid: str) -> None:
    """The job reads `latest`, and every older version is billed, so none is kept."""
    versions = read_json_or_none(
        "secrets", "versions", "list", sid, "--project", project,
        "--filter", "state!=DESTROYED", "--sort-by", "~createTime") or []
    for version in versions[1:]:
        run_quiet("secrets", "versions", "destroy", version["name"].rsplit("/", 1)[1],
                  "--secret", sid, "--project", project, "--quiet")


def secrets(app: dict, action: str, assume_yes: bool, name: str | None = None) -> int:
    project, _region = project_region(app)
    project = preflight(app, project, assume_yes)
    sid = secret_id(app["name"])
    secret = read_json_or_none("secrets", "describe", sid, "--project", project)
    require_managed(secret, f"Secret Manager secret {sid}")
    current = secret_value(project, sid) if secret else None

    def write(values: dict[str, str]) -> None:
        console.step(f"updating secret {sid}")
        run_quiet("secrets", "versions", "add", sid, "--project", project,
                  "--data-file", "-", data=json.dumps(values, sort_keys=True))
        destroy_old_secret_versions(project, sid)

    return run_secrets(action, app, current, write, assume_yes, name)


def deploy(app: dict, assume_yes: bool) -> int:
    name = app["name"]
    project, region = project_region(app)
    project = preflight(app, project, assume_yes)
    billing_confirmed = ensure_apis(project, assume_yes)

    cron = config.cron_expression(app["schedule"])
    timezone = app["timezone"]
    values = gather_secrets(app)
    job = f"pdt-{name}"
    repo = os.environ.get("PDT_ARTIFACT_REGISTRY_REPO", "").strip() or "pdt"
    image = f"{region}-docker.pkg.dev/{project}/{repo}/{name}:latest"
    sa = os.environ.get("PDT_CLOUD_RUN_SERVICE_ACCOUNT", "").strip() \
        or f"pdt-runner@{project}.iam.gserviceaccount.com"
    bucket = store_bucket(project)
    store = deployer_store(project, name) if app["storage"] else None

    console.status(f"Checking current state in project {project} ({region})...")
    repository = read_json_or_none(
        "artifacts", "repositories", "describe", repo,
        "--location", region, "--project", project)
    require_managed(repository, f"Artifact Registry repository {repo}")
    repo_exists = repository is not None
    service_account = service_account_or_none(project, sa)
    default_sa = f"pdt-runner@{project}.iam.gserviceaccount.com"
    if (service_account is not None and sa == default_sa
            and not service_account_owned(service_account)):
        fail(f"service account {sa} exists but is not managed by PDT")
    sa_exists = service_account is not None
    bucket_exists = False
    if store:
        described = read_json_or_none("storage", "buckets", "describe", f"gs://{bucket}")
        require_managed(described, f"bucket {bucket}")
        bucket_exists = described is not None
    usage = (store.usage() if bucket_exists else (0, 0)) if store else None
    run_job = read_json_or_none(
        "run", "jobs", "describe", job, "--region", region, "--project", project)
    require_managed(run_job, f"Cloud Run job {job}")
    job_exists = run_job is not None
    scheduler = read_json_or_none(
        "scheduler", "jobs", "describe", job,
        "--location", region, "--project", project)
    if scheduler is not None and not scheduler_owned(scheduler, name, project, region, sa):
        fail(f"Cloud Scheduler job {job} exists but is not managed by PDT")
    sched_exists = scheduler is not None
    sid = secret_id(name)
    secret = read_json_or_none("secrets", "describe", sid, "--project", project)
    require_managed(secret, f"Secret Manager secret {sid}")
    payload = json.dumps(values, sort_keys=True)
    secret_state = None
    if values:
        if secret is None:
            secret_state = "create"
        else:
            current = secret_value(project, sid)
            secret_state = "unchanged" if current == payload else "update"

    actions = [("use existing" if repo_exists else "create")
               + f" Artifact Registry repo {repo}"]
    actions.append(image_action(app, f"build and push image {image}"))
    if secret_state:
        actions.append(f"{secret_state} secret {sid} ({len(values)} env vars as one json blob)")
    if values:
        actions.append(f"allow {job} to update its own secret {sid}")
    actions.append(("use existing" if sa_exists else "create") + f" service account {sa}")
    if store:
        actions += store_plan_lines(f"bucket {bucket}", bucket_exists, sa, name)
    actions.append(("update" if job_exists else "create") + f" Cloud Run job {job}")
    actions.append(("update" if sched_exists else "create")
                   + f' Cloud Scheduler job {job}: "{cron}" ({timezone})')
    cost = cost_estimate(project, region, cron, job, job_exists,
                         1 if values else 0, assume_yes, billing_confirmed,
                         store_usage=usage)

    if not confirm(actions, assume_yes, cost):
        console.warn("Aborted; nothing was changed.")
        return 1

    if not repo_exists:
        console.step(f"creating Artifact Registry repo {repo}")
        run_quiet("artifacts", "repositories", "create", repo,
                  "--repository-format", "docker", "--location", region,
                  "--project", project, "--labels", "managed-by=pdt")
    console.step(f"building image {image}")
    build_image(app, image, project)
    if not sa_exists:
        if not sa.startswith("pdt-runner@"):
            fail(f"CLOUD_RUN_SERVICE_ACCOUNT {sa} does not exist in project {project}")
        console.step(f"creating service account {sa}")
        run_quiet("iam", "service-accounts", "create", "pdt-runner",
                  "--project", project, "--display-name", "pdt job runner",
                  "--description", "Managed by PDT")
    if store:
        if not bucket_exists:
            console.step(f"creating bucket {bucket}")
            run_quiet("storage", "buckets", "create", f"gs://{bucket}",
                      "--location", region, "--project", project,
                      "--uniform-bucket-level-access", "--public-access-prevention")
            run_quiet("storage", "buckets", "update", f"gs://{bucket}", "--update-labels",
                      ",".join(f"{key}={value}" for key, value in STORE_TAGS.items()))
        console.step(f"granting {sa} write access to {bucket}/{name}/")
        run_quiet("storage", "buckets", "add-iam-policy-binding", f"gs://{bucket}",
                  "--member", f"serviceAccount:{sa}", "--role", STORE_ROLE,
                  "--condition", store_condition(bucket, name))
    if secret_state:
        if secret_state == "create":
            console.step(f"creating secret {sid}")
            run_quiet("secrets", "create", sid, "--project", project,
                      "--replication-policy", "automatic",
                      "--labels", "managed-by=pdt",
                      "--data-file", "-", data=payload)
        elif secret_state == "update":
            console.step(f"updating secret {sid}")
            run_quiet("secrets", "versions", "add", sid, "--project", project,
                      "--data-file", "-", data=payload)
        destroy_old_secret_versions(project, sid)
    if values:
        run_quiet("secrets", "add-iam-policy-binding", sid, "--project", project,
                  "--member", f"serviceAccount:{sa}",
                  "--role", "roles/secretmanager.secretAccessor")
        run_quiet("secrets", "add-iam-policy-binding", sid, "--project", project,
                  "--member", f"serviceAccount:{sa}",
                  "--role", "roles/secretmanager.secretVersionManager")
    console.step(f"deploying Cloud Run job {job}")
    args = ["run", "jobs", "deploy", job, "--image", image, "--region", region,
            "--project", project, "--service-account", sa, "--max-retries", "0",
            "--labels", "managed-by=pdt"]
    if values:
        args += ["--set-secrets", f"PDT_ENV_JSON={sid}:latest"]
    env_vars = []
    if values:
        env_vars.append(f"PDT_ENV_SECRET_RESOURCE=projects/{project}/secrets/{sid}")
    if store:
        env_vars.append(f"PDT_STORAGE_URL={store.url}")
    if env_vars:
        args += ["--set-env-vars", ",".join(env_vars)]
    run_quiet(*args)
    run_quiet("run", "jobs", "add-iam-policy-binding", job, "--region", region,
              "--project", project, "--member", f"serviceAccount:{sa}",
              "--role", "roles/run.invoker")
    console.step(f'scheduling {job}: "{cron}" ({timezone})')
    uri = (f"https://{region}-run.googleapis.com/apis/run.googleapis.com"
           f"/v1/namespaces/{project}/jobs/{job}:run")
    verb = "update" if sched_exists else "create"
    run_quiet("scheduler", "jobs", verb, "http", job,
              "--location", region, "--project", project,
              "--description", scheduler_description(name),
              "--schedule", cron, "--time-zone", timezone,
              "--uri", uri, "--http-method", "POST",
              "--oauth-service-account-email", sa)
    console.done(f"Deployed {name}.")
    console.field("Run it once", f"pdt gcloud run jobs execute {job} --region {region} --project {project}")
    console.field("Run logs", job_logs_url(project, region, job))
    return 0


def destroy(app: dict, assume_yes: bool) -> int:
    name = app["name"]
    project, region = project_region(app)
    project = preflight(app, project, assume_yes)
    ensure_apis(project, assume_yes, DESTROY_APIS)
    job = f"pdt-{name}"
    repo = os.environ.get("PDT_ARTIFACT_REGISTRY_REPO", "").strip() or "pdt"
    image = f"{region}-docker.pkg.dev/{project}/{repo}/{name}"
    sa = os.environ.get("PDT_CLOUD_RUN_SERVICE_ACCOUNT", "").strip() \
        or f"pdt-runner@{project}.iam.gserviceaccount.com"
    default_sa = f"pdt-runner@{project}.iam.gserviceaccount.com"

    console.status(f"Checking current state in project {project} ({region})...")
    scheduler = read_json_or_none(
        "scheduler", "jobs", "describe", job,
        "--location", region, "--project", project)
    delete_scheduler = scheduler_owned(scheduler, name, project, region, sa)
    if scheduler is not None and not delete_scheduler:
        console.note(f"Cloud Scheduler job {job} is not managed by PDT; keeping it")
    run_job = read_json_or_none(
        "run", "jobs", "describe", job,
        "--region", region, "--project", project)
    delete_job = managed_by_pdt(run_job)
    if run_job is not None and not delete_job:
        console.note(f"Cloud Run job {job} is not managed by PDT; keeping it")
    sid = secret_id(name)
    secret = read_json_or_none("secrets", "describe", sid, "--project", project)
    delete_secret = managed_by_pdt(secret)
    if secret is not None and not delete_secret:
        console.note(f"Secret Manager secret {sid} is not managed by PDT; keeping it")
    repository = read_json_or_none(
        "artifacts", "repositories", "describe", repo,
        "--location", region, "--project", project)
    repository_owned = managed_by_pdt(repository)
    if repository is not None and not repository_owned:
        console.note(f"Artifact Registry repository {repo} is not managed by PDT; keeping it")
    images = []
    if repository_owned:
        images = list_json("artifacts", "docker", "images", "list",
                           f"{region}-docker.pkg.dev/{project}/{repo}",
                           "--include-tags", "--project", project)
    app_images = [item for item in images if image_name(item) == name]
    other_images = [item for item in images if image_name(item) != name]
    regional_jobs = list_json("run", "jobs", "list", "--region", region,
                              "--project", project)
    other_regional_jobs = [
        item for item in regional_jobs if run_job_identity(item)[0] != job
    ]
    project_jobs = list_json("run", "jobs", "list", "--project", project)
    other_project_jobs = []
    for item in project_jobs:
        item_name, item_region = run_job_identity(item)
        if item_name == job and item_region == region:
            continue
        other_project_jobs.append(item)
    other_jobs = [
        run_job_identity(item)[0]
        for item in other_project_jobs
        if managed_by_pdt(item) and run_job_identity(item)[0]
    ]
    service_account = service_account_or_none(project, sa)
    bucket = store_bucket(project)
    store = deployer_store(project, name) if app["storage"] else None
    store_present = store is not None and read_json_or_none(
        "storage", "buckets", "describe", f"gs://{bucket}") is not None
    revoke_grant = store_present and store_grant_exists(bucket, name, sa)
    unmanaged_app_resource = ((scheduler is not None and not delete_scheduler)
                              or (run_job is not None and not delete_job))
    delete_repo = (repository_owned and not other_regional_jobs and not other_images
                   and not unmanaged_app_resource)
    delete_image = bool(app_images) and not delete_repo
    delete_sa = (sa == default_sa and service_account_owned(service_account)
                 and not other_project_jobs and not unmanaged_app_resource)

    actions = []
    if revoke_grant:
        actions.append(f"remove {sa} write access to {bucket}/{name}/")
    if delete_scheduler:
        actions.append(f"delete Cloud Scheduler job {job}")
    if delete_job:
        actions.append(f"delete Cloud Run job {job}")
    if delete_secret:
        actions.append(f"delete secret {sid}")
    if delete_image:
        actions.append(f"delete Artifact Registry image {image}")
    if delete_repo:
        actions.append(f"delete Artifact Registry repository {repo} (no other apps use it)")
    if delete_sa:
        actions.append(f"delete service account {sa} (no other apps use it)")
    kept = []
    if scheduler is not None and not delete_scheduler:
        kept.append(f"Cloud Scheduler job {job}")
    if run_job is not None and not delete_job:
        kept.append(f"Cloud Run job {job}")
    if secret is not None and not delete_secret:
        kept.append(f"Secret Manager secret {sid}")
    if repository is not None and not delete_repo:
        kept.append(f"Artifact Registry repository {repo}")
    if service_account is not None and not delete_sa:
        kept.append(f"service account {sa}")
    if store_present:
        kept.append(store_kept_line(f"bucket {bucket}", store.usage()[0], name))
    for item in other_project_jobs:
        if managed_by_pdt(item):
            continue
        item_name, item_region = run_job_identity(item)
        if not item_name:
            continue
        label = f"Cloud Run job {item_name}"
        if item_region:
            label += f" ({item_region})"
        kept.append(label)
    if not actions:
        console.done(f"Nothing to remove for {name} in project {project}.")
        remaining = sorted(set(other_jobs))
        if remaining:
            console.note(f"PDT apps still deployed: {', '.join(remaining)}.")
        if kept:
            console.heading("Still present:")
            for resource in kept:
                console.bullet(f"{resource}")
        return 0
    if revoke_grant:
        warn_if_locked(store, name)
    if not confirm(actions, assume_yes):
        console.warn("Aborted; nothing was changed.")
        return 1

    if revoke_grant:
        console.step(f"removing {sa} write access to {bucket}/{name}/")
        run_quiet("storage", "buckets", "remove-iam-policy-binding", f"gs://{bucket}",
                  "--member", f"serviceAccount:{sa}", "--role", STORE_ROLE,
                  "--condition", store_condition(bucket, name))
    if delete_scheduler:
        console.step(f"deleting Cloud Scheduler job {job}")
        run_quiet("scheduler", "jobs", "delete", job,
                  "--location", region, "--project", project, "--quiet")
    if delete_job:
        console.step(f"deleting Cloud Run job {job}")
        run_quiet("run", "jobs", "delete", job,
                  "--region", region, "--project", project, "--quiet")
    if delete_secret:
        console.step(f"deleting secret {sid}")
        run_quiet("secrets", "delete", sid, "--project", project, "--quiet")
    if delete_image:
        console.step(f"deleting Artifact Registry image {image}")
        run_quiet("artifacts", "docker", "images", "delete", image,
                  "--delete-tags", "--project", project, "--quiet")
    if delete_repo:
        console.step(f"deleting Artifact Registry repository {repo}")
        run_quiet("artifacts", "repositories", "delete", repo,
                  "--location", region, "--project", project, "--quiet")
    if delete_sa:
        console.step(f"deleting service account {sa}")
        run_quiet("iam", "service-accounts", "delete", sa,
                  "--project", project, "--quiet")
    console.done(f"Removed {name} from project {project}.")
    remaining = sorted(set(other_jobs))
    if remaining:
        console.note(f"PDT apps still deployed: {', '.join(remaining)}.")
    if kept:
        console.heading("Still present:")
        for resource in kept:
            console.bullet(f"{resource}")
    elif not remaining:
        console.done("Nothing remains.")
    return 0


def storage(app: dict, rest: list[str], assume_yes: bool) -> int:
    project, _ = project_region(app)
    project = preflight(app, project, assume_yes)
    return storage_cli.run(deployer_store(project, app["name"]), app, rest, assume_yes)


def execution_run(execution: dict) -> runs_cli.Run:
    metadata = execution.get("metadata") or {}
    execution_id = str(execution.get("name") or metadata.get("name") or "").rsplit("/", 1)[-1]
    status = execution.get("status") or {}
    started = datetime.datetime.fromisoformat(status["startTime"])
    completion = status.get("completionTime")
    if not completion:
        return runs_cli.Run(execution_id, started, None, "running")
    ended = datetime.datetime.fromisoformat(completion)
    completed = next((c for c in status.get("conditions") or [] if c.get("type") == "Completed"), None)
    status_text = "succeeded" if (completed or {}).get("status") == "True" else "failed"
    return runs_cli.Run(execution_id, started, ended, status_text)


def list_runs(project: str, region: str, job: str) -> list[runs_cli.Run]:
    executions = describe_json("run", "jobs", "executions", "list", "--job", job,
                               "--region", region, "--project", project)
    if not executions:
        return []
    found = [execution_run(execution) for execution in executions]
    found.sort(key=lambda run: run.started, reverse=True)
    codes = exit_codes(project, job, len(found))
    for run in found:
        run.exit_code = codes.get(run.id)
    return found


def exit_codes(project: str, job: str, count: int) -> dict[str, int]:
    entries = describe_json(
        "logging", "read",
        f'resource.type="cloud_run_job" AND resource.labels.job_name="{job}" AND '
        f'textPayload:"{runs_cli.EXIT_MARKER}"',
        "--project", project, "--limit", str(max(count, 1))) or []
    codes = {}
    for entry in entries:
        execution_id = (entry.get("labels") or {}).get("run.googleapis.com/execution_name")
        code = runs_cli.exit_code([runs_cli.parse_line(entry.get("textPayload") or "", None)])
        if execution_id and code is not None:
            codes[execution_id] = code
    return codes


def read_lines(project: str, execution_id: str) -> list[runs_cli.Line]:
    entries = describe_json(
        "logging", "read",
        f'resource.type="cloud_run_job" AND '
        f'labels."run.googleapis.com/execution_name"="{execution_id}"',
        "--project", project, "--order", "asc", "--limit", "5000")
    if not entries:
        return []
    lines = []
    for entry in entries:
        time = datetime.datetime.fromisoformat(entry["timestamp"]) if entry.get("timestamp") else None
        payload = entry.get("jsonPayload")
        if payload is not None and "message" in payload:
            # Cloud Logging moves a json line's severity out of the payload into the entry.
            lines.append(runs_cli.json_line({**payload, "severity": entry.get("severity", "")},
                                            time))
        else:
            lines.append(runs_cli.parse_line(entry.get("textPayload") or json.dumps(payload),
                                             time))
    return lines


def runs(app: dict, rest: list[str], assume_yes: bool) -> int:
    project, region = project_region(app)
    project = preflight(app, project, assume_yes)
    job = f"pdt-{app['name']}"
    return runs_cli.runs(lambda: list_runs(project, region, job), app["name"], rest)


def logs(app: dict, rest: list[str], assume_yes: bool) -> int:
    project, region = project_region(app)
    project = preflight(app, project, assume_yes)
    job = f"pdt-{app['name']}"
    return runs_cli.logs(lambda: list_runs(project, region, job),
                         lambda run: read_lines(project, run.id), app["name"], rest)


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "gcloud":
        try:
            binary = gcloud_sdk.ensure_gcloud()
        except gcloud_sdk.GcloudError as e:
            fail(str(e))
        return subprocess.run([binary, *sys.argv[2:]]).returncode
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command",
                        choices=("deploy", "destroy", "login", "storage", "secrets", "runs", "logs"))
    parser.add_argument("app")
    parser.add_argument("rest", nargs="*")
    parser.add_argument("--yes", action="store_true")
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
    if args.command == "runs":
        return runs(app, args.rest, args.yes)
    if args.command == "logs":
        return logs(app, args.rest, args.yes)
    if args.command == "secrets":
        return secrets(app, args.rest[0], args.yes, *args.rest[1:])
    if args.command == "deploy":
        return deploy(app, args.yes)
    return destroy(app, args.yes)


if __name__ == "__main__":
    sys.exit(main())
