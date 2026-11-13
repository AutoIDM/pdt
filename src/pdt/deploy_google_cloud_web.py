"""The web profile on Google Cloud: a Cloud Run service behind Identity-Aware Proxy.

Entered through deploy_google_cloud.py, which owns the uv script header, the
gcloud copy, the sign-in, the project, the image build, the shared service
account, the bucket, and the secret code. This module keeps what only a web
app needs.

Resources per app, in the configured project and region:
  Cloud Run service      pdt-<app>      (labels managed-by=pdt, pdt-build=<fingerprint>);
                         IAP on, no unauthenticated caller
  Secret Manager secret  pdt-<app>-env  (as for a job, when the app has env values)
  service account        pdt-web-<app>  the service's identity, for this app only: it may
                         read its own secret, write its own storage folder, and read
                         each storage.read folder, and nothing else
  IAP IAM bindings       roles/iap.httpsResourceAccessor on the service for each
                         web.auth.allowed_domains entry (domain:) and allowed_groups
                         entry (group:); they go with the service
web.resources sets the instance size and scale; RESOURCE_DEFAULTS in web_app.py
fills in each key it does not set.
Shared with the project's jobs: the Artifact Registry repo and the bucket
pdt-data-<suffix>. A web app never runs as the jobs' pdt-runner, so a flaw in it
cannot reach a job's secret or write a job's files.

A web app with `storage.read` gets each named app's folder of the bucket mounted
read-only at /pdt/storage/<app>/ (a Cloud Storage volume, through Cloud Storage
FUSE, limited to that folder with `only-dir`). The service account gets Storage
Object Viewer on each such folder. A file the job writes shows up within about a
minute, the FUSE metadata cache's lifetime.

The URL is known before the first deploy:
https://pdt-<app>-<project number>.<region>.run.app. IAP signs each visitor in
with Google through the Google-managed OAuth client, so nothing is made in the
Cloud console by hand, and forwards the email in X-Goog-Authenticated-User-Email.

`pause` stops the service (manual scaling to 0 instances), and `unpause` sets
automatic scaling again. A deploy keeps a paused app stopped.
`runs` shows the deployed service as one row, so `pdt health` reads it; `logs`
reads what the deployed service wrote since its last deploy: both output streams
and Cloud Run's system log (startup probes, instance starts and stops). IAP on
Cloud Run writes no audit entry per sign-in check, even with the Data Access
audit log on, and a refused request never reaches the service, so no log pdt can
read shows who was turned away.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import re
import os
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

from pdt import console, deploy_google_cloud as gc, regions, runs_cli
from pdt.deploy import DECLINED, confirm, show_plan
from pdt.deploy_common import (
    BUILD_TAG, STORE_TAGS, CostEstimate, build_in_cloud, cached_price, ensure_image, fail,
    fetch_json, gather_secrets, image_action, secret_contents, store_kept_line,
    store_plan_lines, warn_if_locked)
from pdt import web_app

IAP_API = "iap.googleapis.com"
# `gcloud run deploy --iap` reads the project through Resource Manager, and asks to turn the API
# on when it is off; a deploy turns it on first so gcloud asks nothing.
RESOURCE_MANAGER_API = "cloudresourcemanager.googleapis.com"
APIS = (*(api for api in gc.APIS if api != "cloudscheduler.googleapis.com"),
        "monitoring.googleapis.com")
DESTROY_APIS = tuple(api for api in gc.DESTROY_APIS if api != "cloudscheduler.googleapis.com")
IAP_ROLE = "roles/iap.httpsResourceAccessor"
READ_ROLE = "roles/storage.objectViewer"
# Service annotations that mark a stopped service: manual scaling with 0 instances.
SCALING_MODE = "run.googleapis.com/scalingMode"
MANUAL_COUNT = "run.googleapis.com/manualInstanceCount"
APP_CONTAINER = "app"
# The startup probe asks web.health_path every 5 s for up to 90 s.
PROBE = "periodSeconds=5,timeoutSeconds=5,failureThreshold=18"
# A service account ID has 6 to 30 characters.
SA_ID_MAX = 30
SA_DISPLAY_NAME = "pdt web app"
# `gcloud ... --set-env-vars` splits on commas unless the value starts with ^<delimiter>^.
ENV_DELIMITER = "|"
SERVICE_USAGE = "https://serviceusage.googleapis.com/v1beta1"
DIRECTORY = "https://admin.googleapis.com/admin/directory/v1"
REVISION_FILTER = 'resource.type="cloud_run_revision"'
# The service's own output and Cloud Run's system log about it.
# Cloud Run writes its system log under two names: varlog/system and //var/log/system.
SERVICE_LOGS = ('(log_id("run.googleapis.com/stdout") OR log_id("run.googleapis.com/stderr") '
                'OR log_id("run.googleapis.com/varlog/system") '
                'OR log_id("run.googleapis.com//var/log/system"))')


def service_name(app_name: str) -> str:
    return f"pdt-{app_name}"


def repo_name() -> str:
    return os.environ.get("PDT_ARTIFACT_REGISTRY_REPO", "").strip() or "pdt"


def service_account_id(app_name: str) -> str:
    """pdt-web-<app>; a name past the 30-character limit keeps its first 25 characters and
    ends with a hash of the whole name, so two long names do not meet."""
    account = "pdt-web-" + re.sub(r"[^a-z0-9-]", "-", app_name.lower())
    if len(account) <= SA_ID_MAX:
        return account
    return f"{account[:SA_ID_MAX - 5]}-{hashlib.sha256(app_name.encode()).hexdigest()[:4]}"


def service_account(project: str, app_name: str) -> str:
    return f"{service_account_id(app_name)}@{project}.iam.gserviceaccount.com"


def service_account_owned(account: dict | None, app_name: str) -> bool:
    return account is not None and account.get("displayName") == f"{SA_DISPLAY_NAME} {app_name}"


def iap_agent(project_number: str) -> str:
    return f"service-{project_number}@gcp-sa-iap.iam.gserviceaccount.com"


def project_number(project: str) -> str:
    return gc.run_quiet("projects", "describe", project,
                        "--format=value(projectNumber)").strip()


def service_url(app_name: str, number: str, region: str) -> str:
    """The URL Cloud Run gives a service, known before the first deploy."""
    return f"https://{service_name(app_name)}-{number}.{region}.run.app/"


def env_flag(pairs: list[tuple[str, str]]) -> str:
    """One --set-env-vars value whose values may hold commas (a JSON of folder URLs)."""
    return f"^{ENV_DELIMITER}^" + ENV_DELIMITER.join(f"{key}={value}" for key, value in pairs)


def read_urls(app: dict, bucket: str) -> dict[str, str]:
    return {other: gc.store_url(bucket, other) for other in app["storage_read"]}


def iap_members(app: dict) -> list[str]:
    auth = app["auth"]
    return ([f"domain:{domain}" for domain in auth.get("allowed_domains") or []]
            + [f"group:{group}" for group in auth.get("allowed_groups") or []])


def iap_policy_members(project: str, region: str, service: str) -> list[str]:
    policy = gc.describe_json("iap", "web", "get-iam-policy", "--resource-type=cloud-run",
                              "--service", service, "--region", region, "--project", project)
    return [member for binding in (policy or {}).get("bindings") or []
            if binding.get("role") == IAP_ROLE for member in binding.get("members") or []]


def iap_changes(current: list[str], wanted: list[str]) -> tuple[list[str], list[str]]:
    """(members to add, members to remove). pdt removes only the domain: and group: members it
    could have made; a user: or serviceAccount: someone added by hand stays."""
    add = [member for member in wanted if member not in current]
    remove = [member for member in current
              if member not in wanted and member.split(":", 1)[0] in ("domain", "group")]
    return add, remove


def read_grant_exists(bucket: str, other: str, sa: str) -> bool:
    policy = gc.describe_json("storage", "managed-folders", "get-iam-policy",
                              gc.store_url(bucket, other)) or {}
    return any(binding.get("role") == READ_ROLE
               and f"serviceAccount:{sa}" in (binding.get("members") or [])
               for binding in policy.get("bindings") or [])


def held_reads(bucket: str, sa: str, own: str) -> list[str]:
    """The app folders of the bucket, other than the app's own, on which `sa` holds the read
    role: what a deploy or a destroy must check, whatever the config says now."""
    folders = gc.list_json("storage", "managed-folders", "list", f"gs://{bucket}/")
    names = [str(folder.get("name") or "").rstrip("/") for folder in folders]
    return [other for other in names if other and other != own
            and read_grant_exists(bucket, other, sa)]


def grant_store_read(bucket: str, other: str, sa: str) -> None:
    console.step(f"granting {console.value(sa)} read access to "
                 f"{console.value(f'{bucket}/{other}/')}")
    # The other app may not have deployed yet; its folder must exist to carry a grant.
    folder = gc.store_url(bucket, other)
    if gc.describe_json("storage", "managed-folders", "describe", folder) is None:
        gc.run_quiet("storage", "managed-folders", "create", folder)
    gc.run_quiet("storage", "managed-folders", "add-iam-policy-binding",
                 folder, "--member", f"serviceAccount:{sa}", "--role", READ_ROLE)


def revoke_store_read(bucket: str, other: str, sa: str) -> None:
    console.step(f"removing {console.value(sa)} read access to "
                 f"{console.value(f'{bucket}/{other}/')}")
    gc.run_quiet("storage", "managed-folders", "remove-iam-policy-binding",
                 gc.store_url(bucket, other), "--member", f"serviceAccount:{sa}",
                 "--role", READ_ROLE)


@dataclass
class Organization:
    """The project's organization, as far as the deployer may read it."""
    primary: str  # "" for a project with no organization
    domains: set[str]  # lower case: the primary domain and every other domain pdt found
    complete: bool  # the Directory API listed every domain of the Workspace account


def organization(project: str) -> Organization | None:
    """The project's organization, or None when the deployer may not read it."""
    proc = gc.gcloud("projects", "get-ancestors", project, "--format=json")
    if proc.returncode != 0:
        return None
    org = next((item.get("id") for item in json.loads(proc.stdout or "[]")
                if item.get("type") == "organization"), None)
    if org is None:
        return Organization("", set(), True)
    proc = gc.gcloud("organizations", "describe", str(org), "--format=json")
    if proc.returncode != 0:
        return None
    described = json.loads(proc.stdout or "{}")
    primary = described.get("displayName", "")
    customer = (described.get("owner") or {}).get("directoryCustomerId")
    listed = directory_domains(customer) if customer else None
    if listed is not None:
        return Organization(primary, {primary.lower(), *listed}, True)
    # A deployer of an organization's project most often has an account of that organization.
    account = gc.gcloud("auth", "list", "--filter=status:ACTIVE", "--format=value(account)")
    own = account.stdout.strip().rpartition("@")[2].lower()
    return Organization(primary, {primary.lower()} | ({own} if own and not
                                                     own.endswith("gserviceaccount.com") else set()),
                        False)


def directory_domains(customer: str) -> set[str] | None:
    """Every domain of the Workspace account, or None when the deployer may not list them: the
    Directory API needs a Workspace admin and the admin.directory.domain.readonly scope."""
    token = gc.run_quiet("auth", "print-access-token").strip()
    request = urllib.request.Request(f"{DIRECTORY}/customer/{customer}/domains",
                                     headers={"Authorization": f"Bearer {token}"})
    try:
        answer = fetch_json(request) or {}
    except Exception:  # most deployers' tokens lack the scope
        return None
    return {domain["domainName"].lower() for domain in answer.get("domains") or []
            if domain.get("domainName")}


def audience_problems(app: dict, org: Organization | None) -> tuple[list[str], list[str]]:
    """Who the Google-managed OAuth client cannot sign in, as (problems that stop the deploy,
    cautions): it serves only accounts of the project's organization
    (https://docs.cloud.google.com/run/docs/securing/identity-aware-proxy-cloud-run)."""
    if org is None:
        return [], []
    if not org.primary:
        return ["the project has no organization, so Identity-Aware Proxy's Google-managed sign-in "
                "lets no one in; move the project into an organization"], []
    others = [other for other in app["auth"].get("allowed_domains") or []
              if other.lower() not in org.domains]
    if org.complete:
        return [f"{other} is not a domain of the project's organization ({org.primary}), so its "
                f"accounts cannot sign in" for other in others], []
    return [], [f"{other} is not the organization's primary domain ({org.primary}), and pdt may not "
                f"list its other domains; if {other} is not one of them, its accounts cannot sign in"
                for other in others]


def ensure_iap_agent(project: str, number: str) -> str:
    """The IAP service agent's email; Service Usage creates the agent when the project has
    none yet (the gcloud form of this call needs the beta component)."""
    email = iap_agent(number)
    token = gc.run_quiet("auth", "print-access-token").strip()
    request = urllib.request.Request(
        f"{SERVICE_USAGE}/projects/{project}/services/{IAP_API}:generateServiceIdentity",
        data=b"", method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Length": "0"})
    try:
        answer = fetch_json(request) or {}
    except Exception as exc:  # the agent most often exists already; the binding below tells
        console.note(f"could not ask Service Usage for the IAP service agent "
                     f"({console.escape(str(exc))}); using {console.value(email)}")
        return email
    return (answer.get("response") or answer).get("email") or email


def volume_name(other: str) -> str:
    """A Cloud Run volume name for another app's folder: lower case, digits, and hyphens."""
    return "read-" + re.sub(r"[^a-z0-9-]", "-", other.lower())[:50]


def deploy_args(app: dict, project: str, region: str, sa: str, image: str, url: str,
                fingerprint: str, sid: str | None, store_url: str | None, bucket: str) -> list[str]:
    """The `gcloud run deploy` arguments for the service and its one container."""
    service = service_name(app["name"])
    port = str(web_app.port(app))
    size = web_app.resources(app)
    args = ["run", "deploy", service, "--region", region, "--project", project,
            "--service-account", sa, "--platform", "managed", "--execution-environment", "gen2",
            "--ingress", "all", "--scaling", "0" if app["pause"] else "auto",
            "--min-instances", str(size["min_instances"]),
            "--max-instances", str(size["max_instances"]),
            "--concurrency", str(size["concurrency"]), "--timeout", str(size["timeout"]),
            "--labels", f"managed-by=pdt,{BUILD_TAG}={fingerprint}",
            "--no-allow-unauthenticated", "--iap",
            "--image", image, "--port", port, "--cpu", str(size["cpu"]),
            "--memory", f"{web_app.memory_mib(size['memory'])}Mi",
            "--startup-probe", f"httpGet.path={web_app.health_path(app)},httpGet.port={port},{PROBE}",
            # Volumes are set whole on every deploy, so a removed storage.read entry goes too.
            "--clear-volumes", "--clear-volume-mounts"]
    for other in app["storage_read"]:
        volume = volume_name(other)
        args += ["--add-volume", f"name={volume},type=cloud-storage,bucket={bucket},readonly=true,"
                                 f"mount-options=only-dir={other}",
                 "--add-volume-mount", f"volume={volume},mount-path={web_app.STORAGE_READ_DIR}/{other}"]
    env = [(web_app.ENV_PORT, port), (web_app.ENV_URL, url),
           (web_app.ENV_STORAGE_READ_DIR, web_app.STORAGE_READ_DIR)]
    if app["storage_read"]:
        env.append(("PDT_STORAGE_READ_URLS", json.dumps(read_urls(app, bucket), sort_keys=True)))
    if sid:
        env.append(("PDT_ENV_SECRET_RESOURCE", f"projects/{project}/secrets/{sid}"))
    if store_url:
        env.append(("PDT_STORAGE_URL", store_url))
    args += ["--set-env-vars", env_flag(env)]
    # With no secret, a binding from an earlier deploy must go, or the app keeps old values.
    args += ["--set-secrets", f"PDT_ENV_JSON={sid}:latest"] if sid else ["--clear-secrets"]
    return args


MONTH_SECONDS = 730 * 3600
WEEKDAYS_A_MONTH = 21.7
WEEKEND_DAYS_A_MONTH = 8.7
# A new app has no use to measure yet. These defaults come from the first web app pdt ran:
# each visit starts an instance (a cold start), serves about 3 requests, and bills about 7 s.
VISITS_A_WEEKDAY = 20
VISITS_A_WEEKEND_DAY = 2
SECONDS_A_VISIT = 7
REQUESTS_A_VISIT = 3
# How far back the estimate reads the deployed service's use, and the least of a weekday (or of
# a weekend) it needs before it trusts the measured numbers.
HISTORY_DAYS = 14
MIN_COVERED_DAYS = 0.25
FREE_REQUESTS = 2_000_000
FREE_REGISTRY_GIB = 0.5
MONITORING = "https://monitoring.googleapis.com/v3"
REGISTRY = "https://artifactregistry.googleapis.com/v1"
# Cloud Billing catalog descriptions.
CPU_SKU = "Services CPU (Request-based billing)"
MEMORY_SKU = "Services Memory (Request-based billing)"
MIN_CPU_SKU = "Services Min Instance CPU (Request-based billing)"
MIN_MEMORY_SKU = "Services Min Instance Memory (Request-based billing)"
REQUESTS_SKU = "Requests"
REGISTRY_SKU = "Artifact Registry Storage"


@dataclass
class Use:
    """A deployed service's use: billed seconds and requests on weekdays and on weekend days,
    and how many of each it covers (a part day counts as its share of 24 hours)."""
    weekday_seconds: float = 0.0
    weekday_requests: float = 0.0
    weekdays: float = 0.0
    weekend_seconds: float = 0.0
    weekend_requests: float = 0.0
    weekend_days: float = 0.0


def hourly(project: str, service: str, metric: str, start: datetime.datetime,
           end: datetime.datetime, token: str) -> dict[datetime.datetime, float]:
    """The sum of a Cloud Monitoring metric of the service in each hour from start to end."""
    params = urllib.parse.urlencode({
        "filter": f'metric.type="run.googleapis.com/{metric}" '
                  f'AND resource.labels.service_name="{service}"',
        "interval.startTime": start.isoformat(), "interval.endTime": end.isoformat(),
        "aggregation.alignmentPeriod": "3600s", "aggregation.perSeriesAligner": "ALIGN_SUM",
        "aggregation.crossSeriesReducer": "REDUCE_SUM"})
    answer = fetch_json(urllib.request.Request(
        f"{MONITORING}/projects/{project}/timeSeries?{params}",
        headers={"Authorization": f"Bearer {token}"})) or {}
    sums: dict[datetime.datetime, float] = {}
    for series in answer.get("timeSeries") or []:
        for point in series.get("points") or []:
            hour = datetime.datetime.fromisoformat(point["interval"]["endTime"].replace("Z", "+00:00"))
            value = point.get("value") or {}
            sums[hour] = sums.get(hour, 0.0) + float(value.get("doubleValue")
                                                     or value.get("int64Value") or 0)
    return sums


def measured_use(project: str, service: str, deployed: datetime.datetime,
                 now: datetime.datetime | None = None) -> Use:
    """The service's use from Cloud Monitoring, from its first deploy or HISTORY_DAYS ago."""
    end = (now or datetime.datetime.now(datetime.timezone.utc)).replace(
        minute=0, second=0, microsecond=0)
    start = max(deployed, end - datetime.timedelta(days=HISTORY_DAYS))
    use = Use()
    if end - start < datetime.timedelta(hours=1):
        return use
    token = gc.run_quiet("auth", "print-access-token").strip()
    seconds = hourly(project, service, "container/billable_instance_time", start, end, token)
    requests = hourly(project, service, "request_count", start, end, token)
    hour = start.replace(minute=0, second=0, microsecond=0)
    while hour < end:
        share = (min(hour + datetime.timedelta(hours=1), end) - max(hour, start)) / \
            datetime.timedelta(hours=1) / 24
        stamp = hour + datetime.timedelta(hours=1)
        if hour.weekday() < 5:
            use.weekdays += share
            use.weekday_seconds += seconds.get(stamp, 0.0)
            use.weekday_requests += requests.get(stamp, 0.0)
        else:
            use.weekend_days += share
            use.weekend_seconds += seconds.get(stamp, 0.0)
            use.weekend_requests += requests.get(stamp, 0.0)
        hour += datetime.timedelta(hours=1)
    return use


def registry_bytes(project: str, region: str, repo: str) -> int:
    """The size of the image repository; gcloud's JSON leaves it out, the REST answer has it."""
    token = gc.run_quiet("auth", "print-access-token").strip()
    answer = fetch_json(urllib.request.Request(
        f"{REGISTRY}/projects/{project}/locations/{region}/repositories/{repo}",
        headers={"Authorization": f"Bearer {token}"})) or {}
    return int(answer.get("sizeBytes") or 0)


def monthly_use(use: Use | None) -> tuple[float, float, str]:
    """(billed seconds, requests, how pdt got them) for a month."""
    if use is None or use.weekdays + use.weekend_days < MIN_COVERED_DAYS:
        seconds = SECONDS_A_VISIT * (VISITS_A_WEEKDAY * WEEKDAYS_A_MONTH
                                     + VISITS_A_WEEKEND_DAY * WEEKEND_DAYS_A_MONTH)
        return seconds, seconds / SECONDS_A_VISIT * REQUESTS_A_VISIT, "new app"
    # With only weekdays measured, a weekend day is the share of a weekday that a new app gets.
    share = VISITS_A_WEEKEND_DAY / VISITS_A_WEEKDAY
    if use.weekdays >= MIN_COVERED_DAYS:
        day_seconds, day_requests = use.weekday_seconds / use.weekdays, use.weekday_requests / use.weekdays
    else:
        day_seconds = use.weekend_seconds / use.weekend_days / share
        day_requests = use.weekend_requests / use.weekend_days / share
    if use.weekend_days >= MIN_COVERED_DAYS:
        end_seconds, end_requests = use.weekend_seconds / use.weekend_days, use.weekend_requests / use.weekend_days
    else:
        end_seconds, end_requests = day_seconds * share, day_requests * share
    return (day_seconds * WEEKDAYS_A_MONTH + end_seconds * WEEKEND_DAYS_A_MONTH,
            day_requests * WEEKDAYS_A_MONTH + end_requests * WEEKEND_DAYS_A_MONTH, "measured")


# Seconds between reads of the Cloud Billing catalog after a failed read.
CATALOG_WAITS = (10, 20, 40)


def cost_estimate(app: dict, project: str, region: str, use: Use | None = None,
                  registry_bytes: int = 0) -> CostEstimate | None:
    """Monthly cost from the service's measured use, or from the defaults for a new app, at the
    Cloud Billing catalog's list prices for the region; None when the catalog does not answer
    after CATALOG_WAITS."""
    currency = regions.local_currency()

    def price(service: str, description: str, in_currency: str) -> float:
        def read() -> list:
            ids = {s.get("displayName"): s.get("serviceId")
                   for s in gc.billing_list("services", "services", project)}
            skus = gc.billing_list(f"services/{ids[service]}/skus", "skus", project,
                                   in_currency)
            return list(gc.sku_price(skus, region, description))

        amount, _unit = cached_price("google-cloud", region, in_currency,
                                     f"{service}: {description}", read,
                                     "reading: prices from the Cloud Billing catalog")
        return amount

    size = web_app.resources(app)
    vcpu, gib = float(size["cpu"]), web_app.memory_mib(size["memory"]) / 1024
    seconds, requests, source = monthly_use(use)
    for wait in (0, *CATALOG_WAITS):
        time.sleep(wait)
        for in_currency in dict.fromkeys((currency, "USD")):
            try:
                cpu, memory = price("Cloud Run", CPU_SKU, in_currency), price("Cloud Run", MEMORY_SKU,
                                                                              in_currency)
                request = price("Cloud Run", REQUESTS_SKU, in_currency)
                stored = price("Artifact Registry", REGISTRY_SKU, in_currency)
                idle = 0.0
                if size["min_instances"]:
                    idle = size["min_instances"] * MONTH_SECONDS * (
                        vcpu * price("Cloud Run", MIN_CPU_SKU, in_currency)
                        + gib * price("Cloud Run", MIN_MEMORY_SKU, in_currency))
            except Exception as exc:  # the billing catalog is a nicety; the deploy goes on
                detail = gc.billing_detail(exc)
                continue
            items = [(f"Cloud Run: {seconds / 60:,.0f} min a month ({source})",
                      seconds * (vcpu * cpu + gib * memory))]
            if size["min_instances"]:
                items.append((f"{size['min_instances']} idle instance(s) kept running", idle))
            items.append((f"Requests: {requests:,.0f} a month (2 million free)",
                          max(0.0, requests - FREE_REQUESTS) * request))
            items.append(("Identity-Aware Proxy", 0.0))
            registry_gib = registry_bytes / 1024 ** 3
            items.append((f"Artifact Registry: {registry_gib:.2f} GiB of images, shared "
                          "(0.5 GiB free)", max(0.0, registry_gib - FREE_REGISTRY_GIB) * stored))
            converted = "" if in_currency == "USD" else ", converted from USD by Google Cloud"
            return CostEstimate(items, f"{region} list prices{converted}, {vcpu:g} vCPU / {gib:g} GiB "
                                "billed per second while a request runs, before the Cloud Run "
                                "free tier", "excludes network egress", currency=in_currency)
    console.warn(f"No cost estimate: the Cloud Billing catalog did not answer"
                 f"{': ' + console.escape(detail) if detail else ''}")
    return None


def deploy(app: dict, assume_yes: bool, plan_only: bool = False) -> int:
    cloud = build_in_cloud(app, "Cloud Build", "linux/amd64")
    name = app["name"]
    project, region = gc.project_region(app)
    console.status("Checking your Google Cloud sign-in...")
    project = gc.preflight(app, project, assume_yes)
    gate = web_app.gate(app, "google-cloud")
    apis = (*APIS, *((IAP_API, RESOURCE_MANAGER_API) if gate else ()), *((gc.CLOUD_BUILD_API,) if cloud else ()))
    gc.ensure_apis(project, assume_yes, apis)
    if gate:
        stops, cautions = audience_problems(app, organization(project))
        if stops:
            fail("web.auth: " + "; ".join(console.escape(problem) for problem in stops))
        for caution in cautions:
            console.warn(f"web.auth: {console.escape(caution)}")

    service = service_name(name)
    values = gather_secrets(app)
    repo = repo_name()
    image = f"{region}-docker.pkg.dev/{project}/{repo}/{name}:latest"
    sa = service_account(project, name)
    bucket = gc.store_bucket(project)
    store = gc.deployer_store(project, name) if app["storage"] else None
    needs_bucket = bool(app["storage"] or app["storage_read"])

    console.status(f"Checking current state in project {console.value(project)} "
                   f"({console.value(region)})...")
    with console.waiting("reading: resources"):
        number = project_number(project)
        url = service_url(name, number, region)
        repository = gc.read_json_or_none("artifacts", "repositories", "describe", repo,
                                          "--location", region, "--project", project)
        gc.require_managed(repository, f"Artifact Registry repository {console.value(repo)}")
        repo_exists = repository is not None
        account = gc.service_account_or_none(project, sa)
        if account is not None and not service_account_owned(account, name):
            fail(f"service account {console.value(sa)} exists but is not managed by PDT")
        sa_exists = account is not None
        builder = gc.builder_email(project)
        builds = gc.build_bucket(project)
        builder_exists = builds_exists = False
        if cloud:
            builder_account = gc.service_account_or_none(project, builder)
            if builder_account is not None and not gc.builder_owned(builder_account):
                fail(f"service account {console.value(builder)} exists but is not managed by PDT")
            builder_exists = builder_account is not None
            described = gc.read_json_or_none("storage", "buckets", "describe", f"gs://{builds}")
            gc.require_managed(described, f"bucket {console.value(builds)}")
            builds_exists = described is not None
        described = gc.read_json_or_none("storage", "buckets", "describe", f"gs://{bucket}")
        gc.require_managed(described, f"bucket {console.value(bucket)}")
        bucket_exists = described is not None
        # Grants an earlier deploy made that the config no longer names.
        stale_reads = [other for other in (held_reads(bucket, sa, name) if bucket_exists else [])
                       if other not in app["storage_read"]]
        stale_folder = (bucket_exists and not app["storage"]
                        and gc.folder_grant_exists(bucket, name, sa))
        usage = (store.usage() if bucket_exists else (0, 0)) if store else None
        gc.refuse_profile_change(app, project, region, "deploy")
        current = gc.read_json_or_none("run", "services", "describe", service,
                                       "--region", region, "--project", project)
        gc.require_managed(current, f"Cloud Run service {console.value(service)}")
        service_exists = current is not None
        deployed = (((current or {}).get("metadata") or {}).get("labels") or {}).get(BUILD_TAG)
        sid = gc.secret_id(name)
        secret = gc.read_json_or_none("secrets", "describe", sid, "--project", project)
        gc.require_managed(secret, f"Secret Manager secret {console.value(sid)}")
        payload = json.dumps(values, sort_keys=True)
        secret_state = None
        if values:
            if secret is None:
                secret_state = "create"
            else:
                secret_state = "unchanged" if gc.secret_value(project, sid) == payload else "update"
        members = iap_policy_members(project, region, service) if service_exists and gate else []

        add_members, remove_members = iap_changes(members, iap_members(app) if gate else [])

    actions = [console.Markup(("use existing" if repo_exists else "create")
                              + f" Artifact Registry repo {console.value(repo)}")]
    set_cleanup = not gc.cleanup_policies_set(repository)
    if set_cleanup:
        actions.append(console.Markup(f"set the clean-up policy of {console.value(repo)}: "
                                      f"{console.escape(gc.CLEANUP_LINE)}"))
    if cloud:
        actions.append(console.Markup(("use existing" if builder_exists else "create")
                                      + f" service account {console.value(builder)} for "
                                      "Cloud Build"))
        actions.append(console.Markup(("use existing" if builds_exists else "create")
                                      + f" bucket {console.value(builds)} for the Cloud Build "
                                      "upload and log (objects deleted after 1 day)"))
    actions.append(image_action(app, console.Markup(
        f"build and push image {console.value(image)}"), "Cloud Build" if cloud else ""))
    if secret_state:
        actions.append(console.Markup(f"{secret_state} secret {console.value(sid)} "
                                      f"({secret_contents(values)}, as one json blob)"))
    actions.append(console.Markup(("use existing" if sa_exists else "create")
                                  + f" service account {console.value(sa)}"))
    if store:
        actions += store_plan_lines(f"bucket {console.value(bucket)}", bucket_exists, sa, name)
    for other in app["storage_read"]:
        actions.append(console.Markup(f"allow {console.value(sa)} to read "
                                      f"{console.value(f'{bucket}/{other}/')}, mounted read-only at "
                                      f"{console.value(f'{web_app.STORAGE_READ_DIR}/{other}/')}"))
    for other in stale_reads:
        actions.append(console.Markup(f"remove {console.value(sa)} read access to "
                                      f"{console.value(f'{bucket}/{other}/')}"))
    if stale_folder:
        actions.append(console.Markup(f"remove {console.value(sa)} write access to "
                                      f"{console.value(f'{bucket}/{name}/')}"))
    size = web_app.resources(app)
    actions.append(console.Markup(
        ("update" if service_exists else "create")
        + f" Cloud Run service {console.value(service)}: {console.value(str(size['cpu']))} vCPU, "
        f"{console.value(size['memory'])}, {console.value(str(size['min_instances']))} to "
        f"{console.value(str(size['max_instances']))} instances, "
        f"{console.value(str(size['concurrency']))} requests each, "
        f"{console.value(str(size['timeout']))} s timeout, port "
        f"{console.value(str(web_app.port(app)))}, health check on "
        f"{console.value(web_app.health_path(app))}"
        + (" (paused)" if app["pause"] else "")))
    if gate:
        actions.append(console.Markup(f"put {console.value(gate)} in front of {console.value(url)}"))
        for member in add_members:
            actions.append(console.Markup(f"allow {console.value(member)} to enter"))
        for member in remove_members:
            actions.append(console.Markup(f"no longer allow {console.value(member)} to enter"))
    use = None
    if service_exists:
        try:
            use = measured_use(project, service, datetime.datetime.fromisoformat(
                ((current.get("metadata") or {}).get("creationTimestamp") or "")
                .replace("Z", "+00:00")))
        except Exception as exc:  # Cloud Monitoring is a nicety; the estimate uses the defaults
            console.note(f"could not read the use of {console.value(service)} from Cloud "
                         f"Monitoring ({console.escape(str(exc))}); the estimate uses the "
                         "defaults for a new app")
    stored = 0
    if repo_exists:
        try:
            stored = registry_bytes(project, region, repo)
        except Exception:  # the repository's size is a nicety; the estimate shows 0
            pass
    cost = cost_estimate(app, project, region, use, stored)

    if plan_only:
        return show_plan(actions, cost)
    if not confirm(actions, assume_yes, cost):
        console.warn("Aborted; nothing was changed.")
        return DECLINED

    if not repo_exists:
        console.step(f"creating Artifact Registry repo {console.value(repo)}")
        gc.run_quiet("artifacts", "repositories", "create", repo,
                     "--repository-format", "docker", "--location", region,
                     "--project", project, "--labels", "managed-by=pdt",
                     slow=f"creating: Artifact Registry repository {console.value(repo)}")
    if set_cleanup:
        gc.set_cleanup_policies(project, region, repo)
    containers = ((((current or {}).get("spec") or {}).get("template") or {}).get("spec") or {}
                  ).get("containers") or []
    pinned = next((c.get("image") for c in containers
                   if c.get("name", APP_CONTAINER) == APP_CONTAINER), None)

    def build(stage: Path) -> None:
        nonlocal pinned
        if cloud:
            gc.ensure_builder(project, region, repo, builder, builds, builder_exists,
                              builds_exists)
            gc.cloud_build_image(stage, image, project, region, builder, builds)
        else:
            gc.build_image(stage, image, region)
        pinned = gc.pushed_image(image, project)
        if pinned is None:
            fail(f"Artifact Registry holds no image {console.value(image)} after the build")

    fingerprint = ensure_image(app, image, "linux/amd64", "Cloud Build" if cloud else "",
                               deployed, lambda: gc.pushed_image(image, project) == pinned, build)
    if not sa_exists:
        console.step(f"creating service account {console.value(sa)}")
        gc.run_quiet("iam", "service-accounts", "create", service_account_id(name),
                     "--project", project, "--display-name", f"{SA_DISPLAY_NAME} {name}",
                     "--description", "Managed by PDT")
    if needs_bucket and not bucket_exists:
        console.step(f"creating bucket {console.value(bucket)}")
        gc.run_quiet("storage", "buckets", "create", f"gs://{bucket}",
                     "--location", region, "--project", project,
                     "--uniform-bucket-level-access", "--public-access-prevention")
        gc.run_quiet("storage", "buckets", "update", f"gs://{bucket}", "--update-labels",
                     ",".join(f"{key}={value}" for key, value in STORE_TAGS.items()))
    if store:
        gc.grant_store_access(bucket, name, sa)
    for other in app["storage_read"]:
        grant_store_read(bucket, other, sa)
    for other in stale_reads:
        revoke_store_read(bucket, other, sa)
    if stale_folder:
        gc.revoke_store_access(bucket, name, sa, True, False)
    if secret_state == "create":
        console.step(f"creating secret {console.value(sid)}")
        gc.run_quiet("secrets", "create", sid, "--project", project,
                     "--replication-policy", "automatic", "--labels", "managed-by=pdt",
                     "--data-file", "-", data=payload)
    elif secret_state == "update":
        console.step(f"updating secret {console.value(sid)}")
        gc.run_quiet("secrets", "versions", "add", sid, "--project", project,
                     "--data-file", "-", data=payload)
        gc.destroy_old_secret_versions(project, sid)
    if values:
        for role in ("roles/secretmanager.secretAccessor",
                     "roles/secretmanager.secretVersionManager"):
            gc.run_quiet("secrets", "add-iam-policy-binding", sid, "--project", project,
                         "--member", f"serviceAccount:{sa}", "--role", role)
    if gate:
        agent = ensure_iap_agent(project, number)
    console.step(f"deploying Cloud Run service {console.value(service)}")
    if any(c.get("name") == "storage-sync" for c in containers):
        fail(f"Cloud Run service {console.value(service)} still has the storage sidecar of an "
             f"earlier pdt; run {console.value(f'pdt destroy {name}')}, then deploy again")
    args = deploy_args(app, project, region, sa, pinned, url, fingerprint,
                       sid if values else None, store.url if store else None, bucket)
    gc.run_quiet(*args, slow=f"deploying: Cloud Run service {console.value(service)}")
    described = gc.read_json_or_none("run", "services", "describe", service,
                                     "--region", region, "--project", project) or {}
    # status.url is the older hash URL; status.urls also lists the deterministic one, which the
    # console shows and PDT_URL already holds.
    urls = (described.get("status") or {}).get("urls") or []
    if urls and url.rstrip("/") not in urls:
        console.note(f"Cloud Run lists {console.value(', '.join(urls))}, not "
                     f"{console.value(url)}; the app's PDT_URL may be wrong")
    if gate:
        console.step(f"letting {console.value(gate)} call {console.value(service)}")
        gc.run_quiet("run", "services", "add-iam-policy-binding", service, "--region", region,
                     "--project", project, "--member", f"serviceAccount:{agent}",
                     "--role", "roles/run.invoker")
        for member in add_members:
            console.step(f"allowing {console.value(member)} to enter")
            gc.run_quiet("iap", "web", "add-iam-policy-binding", "--resource-type=cloud-run",
                         "--service", service, "--region", region, "--project", project,
                         "--member", member, "--role", IAP_ROLE)
        for member in remove_members:
            console.step(f"no longer allowing {console.value(member)} to enter")
            gc.run_quiet("iap", "web", "remove-iam-policy-binding", "--resource-type=cloud-run",
                         "--service", service, "--region", region, "--project", project,
                         "--member", member, "--role", IAP_ROLE)
    console.done(f"Deployed {console.value(name)}: {console.value(url)}")
    if app["pause"]:
        console.note(f"{console.value(name)} is paused; {console.value(f'pdt unpause {name}')} "
                     "starts it")
    return 0


def destroy(app: dict, assume_yes: bool) -> int:
    name = app["name"]
    project, region = gc.project_region(app)
    console.status("Checking your Google Cloud sign-in...")
    project = gc.preflight(app, project, assume_yes)
    gc.ensure_apis(project, assume_yes, DESTROY_APIS)
    service = service_name(name)
    repo = repo_name()
    image = f"{region}-docker.pkg.dev/{project}/{repo}/{name}"
    sa = service_account(project, name)
    bucket = gc.store_bucket(project)
    sid = gc.secret_id(name)

    console.status(f"Checking current state in project {console.value(project)} "
                   f"({console.value(region)})...")
    with console.waiting("reading: resources"):
        gc.refuse_profile_change(app, project, region, "destroy")
        current = gc.read_json_or_none("run", "services", "describe", service,
                                       "--region", region, "--project", project)
        delete_service = gc.managed_by_pdt(current)
        account = gc.service_account_or_none(project, sa)
        delete_account = service_account_owned(account, name)
        if current is not None and not delete_service:
            console.note(f"Cloud Run service {console.value(service)} is not managed by PDT; "
                         "keeping it")
        secret = gc.read_json_or_none("secrets", "describe", sid, "--project", project)
        delete_secret = gc.managed_by_pdt(secret)
        if secret is not None and not delete_secret:
            console.note(f"Secret Manager secret {console.value(sid)} is not managed by PDT; "
                         "keeping it")
        repository = gc.read_json_or_none("artifacts", "repositories", "describe", repo,
                                          "--location", region, "--project", project)
        images = []
        if gc.managed_by_pdt(repository):
            images = gc.list_json("artifacts", "docker", "images", "list",
                                  f"{region}-docker.pkg.dev/{project}/{repo}",
                                  "--include-tags", "--project", project)
        other_images = [item for item in images if gc.image_name(item) != name]
        delete_repo = gc.managed_by_pdt(repository) and not other_images
        delete_image = any(gc.image_name(item) == name for item in images) and not delete_repo
        # The builder and the build bucket go with the last of the project's jobs and web apps.
        last_app = (not [item for item in gc.list_json("run", "jobs", "list", "--project", project)
                         if gc.managed_by_pdt(item)]
                    and not [other for other in gc.pdt_services(project) if other != service])
        builder = gc.builder_email(project)
        builder_account = gc.service_account_or_none(project, builder)
        builds = gc.build_bucket(project)
        builds_bucket = gc.read_json_or_none("storage", "buckets", "describe", f"gs://{builds}")
        delete_builder = last_app and gc.builder_owned(builder_account)
        delete_builds = last_app and gc.managed_by_pdt(builds_bucket)
        store = gc.deployer_store(project, name) if app["storage"] else None
        bucket_present = gc.read_json_or_none("storage", "buckets", "describe",
                                              f"gs://{bucket}") is not None
        revoke_folder = bucket_present and gc.folder_grant_exists(bucket, name, sa)
        revoke_bucket = bucket_present and gc.bucket_grant_exists(bucket, name, sa)
        revoke_reads = held_reads(bucket, sa, name) if bucket_present else []

    actions = []
    if revoke_folder or revoke_bucket:
        actions.append(console.Markup(f"remove {console.value(sa)} write access to "
                                      f"{console.value(f'{bucket}/{name}/')}"))
    for other in revoke_reads:
        actions.append(console.Markup(f"remove {console.value(sa)} read access to "
                                      f"{console.value(f'{bucket}/{other}/')}"))
    if delete_service:
        actions.append(console.Markup(f"delete Cloud Run service {console.value(service)} and "
                                      "its sign-in bindings"))
    if delete_secret:
        actions.append(console.Markup(f"delete secret {console.value(sid)}"))
    if delete_image:
        actions.append(console.Markup(f"delete Artifact Registry image {console.value(image)}"))
    if delete_account:
        actions.append(console.Markup(f"delete service account {console.value(sa)}"))
    if delete_repo:
        actions.append(console.Markup(f"delete Artifact Registry repository {console.value(repo)}"))
    if delete_builder:
        actions.append(console.Markup(f"delete service account {console.value(builder)}"))
    if delete_builds:
        actions.append(console.Markup(f"delete bucket {console.value(builds)}"))
    kept = []
    if current is not None and not delete_service:
        kept.append(f"Cloud Run service {console.value(service)}")
    if account is not None and not delete_account:
        kept.append(f"service account {console.value(sa)} (not managed by PDT)")
    if repository is not None and not delete_repo:
        kept.append(f"Artifact Registry repository {console.value(repo)} (shared)")
    if bucket_present and store is not None:
        kept.append(store_kept_line(f"bucket {console.value(bucket)}", store.usage()[0], name))
    if not actions:
        console.done(f"Nothing to remove for {console.value(name)} in project "
                     f"{console.value(project)}.")
        if kept:
            console.heading("Still present:")
            for resource in kept:
                console.bullet(resource)
        return 0
    if store is not None and (revoke_folder or revoke_bucket):
        warn_if_locked(store, name)
    if not confirm(actions, assume_yes):
        console.warn("Aborted; nothing was changed.")
        return DECLINED

    if revoke_folder or revoke_bucket:
        gc.revoke_store_access(bucket, name, sa, revoke_folder, revoke_bucket)
    for other in revoke_reads:
        revoke_store_read(bucket, other, sa)
    if delete_service:
        console.step(f"deleting Cloud Run service {console.value(service)}")
        gc.run_quiet("run", "services", "delete", service, "--region", region,
                     "--project", project, "--quiet",
                     slow=f"deleting: Cloud Run service {console.value(service)}")
    if delete_secret:
        console.step(f"deleting secret {console.value(sid)}")
        gc.run_quiet("secrets", "delete", sid, "--project", project, "--quiet")
    if delete_image:
        console.step(f"deleting Artifact Registry image {console.value(image)}")
        gc.run_quiet("artifacts", "docker", "images", "delete", image,
                     "--delete-tags", "--project", project, "--quiet",
                     slow=f"deleting: Artifact Registry image {console.value(image)}")
    if delete_account:
        console.step(f"deleting service account {console.value(sa)}")
        gc.run_quiet("iam", "service-accounts", "delete", sa, "--project", project, "--quiet")
    if delete_repo:
        console.step(f"deleting Artifact Registry repository {console.value(repo)}")
        gc.run_quiet("artifacts", "repositories", "delete", repo, "--location", region,
                     "--project", project, "--quiet",
                     slow=f"deleting: Artifact Registry repository {console.value(repo)}")
    if delete_builder:
        console.step(f"deleting service account {console.value(builder)}")
        gc.run_quiet("iam", "service-accounts", "delete", builder, "--project", project, "--quiet")
    if delete_builds:
        console.step(f"deleting bucket {console.value(builds)}")
        gc.run_quiet("storage", "rm", "--recursive", f"gs://{builds}/", "--project", project,
                     slow=f"deleting: bucket {console.value(builds)}")
    console.done(f"Removed {console.value(name)} from project {console.value(project)}.")
    if kept:
        console.heading("Still present:")
        for resource in kept:
            console.bullet(resource)
    return 0


def deployed_runs(project: str, region: str, service: str) -> list[runs_cli.Run]:
    """The deployed service as one run: started at its last deploy, running while it is
    ready. [] when nothing is deployed."""
    current = gc.read_json_or_none("run", "services", "describe", service,
                                   "--region", region, "--project", project)
    if current is None:
        return []
    conditions = {c.get("type"): c for c in (current.get("status") or {}).get("conditions") or []}
    ready = conditions.get("Ready") or {}
    revision = gc.read_json_or_none(
        "run", "revisions", "describe",
        (current.get("status") or {}).get("latestCreatedRevisionName") or "",
        "--region", region, "--project", project) \
        if (current.get("status") or {}).get("latestCreatedRevisionName") else None
    when = (((revision or {}).get("metadata") or {}).get("creationTimestamp")
            or (current.get("metadata") or {}).get("creationTimestamp"))
    status = {"True": "running", "False": "failed"}.get(ready.get("status"), "running")
    annotations = (current.get("metadata") or {}).get("annotations") or {}
    if annotations.get(SCALING_MODE) == "manual" and str(annotations.get(MANUAL_COUNT)) == "0":
        status = "stopped"
    return [runs_cli.Run(service, datetime.datetime.fromisoformat(when), None, status)]


def service_filter(service: str) -> str:
    return f'{REVISION_FILTER} AND resource.labels.service_name="{service}" AND {SERVICE_LOGS}'


def service_line(entry: dict) -> runs_cli.Line:
    line = gc.entry_line(entry)
    if entry.get("logName", "").endswith(("varlog%2Fsystem", "%2Fvar%2Flog%2Fsystem")):
        line.message = f"[system] {line.message}"
    return line


def read_lines(project: str, service: str, since: datetime.datetime | None = None
               ) -> list[runs_cli.Line]:
    after = "" if since is None else f' AND timestamp>="{since.isoformat()}"'
    proc = gc.gcloud("logging", "read", service_filter(service) + after, "--project", project,
                     "--order", "asc", "--format=json")
    if proc.returncode != 0:
        console.warn_once(f"Cloud Logging did not return the lines of {console.value(service)}: "
                          f"{console.escape(proc.stderr.strip())}")
        return []
    return [service_line(entry) for entry in json.loads(proc.stdout or "null") or []]


def stream_lines(project: str, service: str, run: runs_cli.Run,
                 follow: bool) -> Iterator[runs_cli.Line]:
    if not follow:
        yield from read_lines(project, service, run.started)
        return
    stop = threading.Event()
    tail = gc.tail_entries(project, service_filter(service), stop)
    try:
        yield from runs_cli.store_and_tail(
            lambda since: read_lines(project, service, since or run.started),
            (service_line(entry) for entry in tail), gc.HOLD)
    finally:
        stop.set()


def service_page(project: str, region: str, service: str, tab: str = "") -> str:
    return (f"https://console.cloud.google.com/run/detail/{region}/{service}/{tab}"
            f"?project={project}")


def runs(app: dict, rest: list[str], assume_yes: bool) -> int:
    project, region = gc.project_region(app)
    project = gc.preflight(app, project, assume_yes)
    service = service_name(app["name"])
    return runs_cli.runs(lambda: deployed_runs(project, region, service), app["name"], rest,
                         page=service_page(project, region, service),
                         note="The deployed service; it started at its last deploy.")


def logs(app: dict, rest: list[str], assume_yes: bool,
         new: Callable[[runs_cli.Run], bool] | None = None) -> int:
    project, region = gc.project_region(app)
    project = gc.preflight(app, project, assume_yes)
    service = service_name(app["name"])
    return runs_cli.logs(lambda: deployed_runs(project, region, service),
                         lambda run, since: read_lines(project, service, since or run.started),
                         app["name"], rest, store="Cloud Logging", delay=runs_cli.LOG_DELAY,
                         hold=gc.HOLD,
                         stream=lambda run, follow: stream_lines(project, service, run, follow),
                         new=new,
                         place=runs_cli.LogPlace(
                             "Cloud Logging", f"project {project}, by service name", "Service",
                             lambda runs: [run.id for run in runs],
                             lambda: service_page(project, region, service, "logs"),
                             lambda revision: "https://console.cloud.google.com/logs/query;query="
                             f"{urllib.parse.quote(service_filter(service), safe='')}"
                             f"?project={project}"))


def pause(app: dict, paused: bool, assume_yes: bool) -> int:
    project, region = gc.project_region(app)
    project = gc.preflight(app, project, assume_yes)
    service = service_name(app["name"])
    current = gc.read_json_or_none("run", "services", "describe", service,
                                   "--region", region, "--project", project)
    if current is None:
        fail(f"Cloud Run service {console.value(service)} does not exist; run "
             f"{console.value(f'pdt deploy {app['name']}')} first")
    gc.run_quiet("run", "services", "update", service, "--region", region, "--project", project,
                 "--scaling", "0" if paused else "auto",
                 slow=f"{'stopping' if paused else 'starting'}: Cloud Run service "
                      f"{console.value(service)}")
    console.done(f"{'Paused' if paused else 'Unpaused'} {console.value(app['name'])}: Cloud Run "
                 f"service {console.value(service)} is {'stopped' if paused else 'started'}.")
    return 0


def start(app: dict, assume_yes: bool, follow: bool = False) -> int:
    """`pdt run APP --deployed` opens the deployed app in the browser."""
    project, region = gc.project_region(app)
    project = gc.preflight(app, project, assume_yes)
    url = service_url(app["name"], project_number(project), region)
    console.done(f"Opening {console.value(url)}")
    webbrowser.open(url)
    return 0


def command(app: dict, name: str, rest: list[str], assume_yes: bool, plan_only: bool) -> int:
    """One provider command for a web app; `deploy_google_cloud.main` calls it."""
    if name == "runs":
        return runs(app, rest, assume_yes)
    if name == "logs":
        return logs(app, rest, assume_yes)
    if name in ("pause", "unpause"):
        return pause(app, name == "pause", assume_yes)
    if name == "start":
        return start(app, assume_yes, "--follow" in rest)
    if name == "secrets":
        return gc.secrets(app, rest[0], assume_yes, *rest[1:])
    if name == "deploy":
        return deploy(app, assume_yes, plan_only)
    return destroy(app, assume_yes)
