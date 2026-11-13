"""The `web` profile: an HTTP server that runs all the time behind the platform's sign-in.

The app is any container that listens on the port in `web.port` (PDT_PORT), or a
`run.py` that does. It needs no sign-in code. The platform's own gate stands in
front of it and lets no request through until the visitor signed in: Identity-Aware
Proxy on Cloud Run. `web.auth.provider` names where visitors sign in (`google`). A
web app is never open to everyone: `pdt validate` requires a provider and at least
one of `web.auth.allowed_domains` and `web.auth.allowed_groups`. The gate sets a
header with the visitor's email (`pdt.utils.auth.current_user` reads it), and the
app may ignore it.

`web.auth.allowed_domains` and `web.auth.allowed_groups` say who may enter.

`web.resources` sizes the deployed app: cpu, memory, timeout (seconds a request
may take), min_instances, max_instances, and concurrency (requests one instance
serves at once). Each key that is not set takes RESOURCE_DEFAULTS.

`storage.read` names the apps whose files this app may read. The deployed app
finds each one's folder read-only under STORAGE_READ_DIR/<app>/, the way the
platform does it best (a Cloud Storage volume on Cloud Run), and the folder URLs
in PDT_STORAGE_READ_URLS.
"""

from __future__ import annotations

NAME = "web"
DESCRIPTION = "an HTTP server that runs all the time behind the platform's sign-in"

KEYS = {"web"}
WEB_KEYS = {"port", "health_path", "auth", "resources"}
AUTH_KEYS = {"provider", "allowed_domains", "allowed_groups"}
# What a deployed web app gets when web.resources does not say.
RESOURCE_DEFAULTS = {"cpu": 1, "memory": "512MiB", "timeout": 300, "min_instances": 0,
                     "max_instances": 3, "concurrency": 80}
# The vCPU counts a Cloud Run instance can have, and the least and the most memory, in MiB,
# each allows (https://docs.cloud.google.com/run/docs/configuring/services/memory-limits).
CPU_MEMORY = {1: (512, 4096), 2: (512, 8192), 4: (2048, 16384), 6: (4096, 24576),
              8: (4096, 32768)}
TIMEOUT_MAX = 3600
CONCURRENCY_MAX = 1000
AUTH_PROVIDERS = ("google",)
DEFAULT_PORT = 8080
DEFAULT_HEALTH_PATH = "/"
# The providers that can run this profile today. The others are refused at validation
# time, so a deploy never gets as far as the cloud.
PROVIDERS = ("google-cloud",)
# Which sign-in gate each (provider, web.auth.provider) pair gets.
GATES = {
    ("google-cloud", "google"): "Identity-Aware Proxy",
}

# What the app reads.
ENV_PORT = "PDT_PORT"
ENV_URL = "PDT_URL"
ENV_STORAGE_READ_DIR = "PDT_STORAGE_READ_DIR"
# Where the deployed app finds a read-only copy of each `storage.read` app's files.
STORAGE_READ_DIR = "/pdt/storage"

COMMANDS = {
    "run": "start the app on this computer with no sign-in, from run.py or its Dockerfile; "
           "Ctrl+C stops",
    "run --deployed": "open the deployed app in the browser",
    "runs": "one row: the deployed service, since its last deploy",
    "logs": "what the deployed service wrote since its last deploy",
    "health": "whether the deployed service serves, is stopped, or failed",
    "pause": "stop the server; `unpause` starts it again",
    "deploy": "build the app, create or update the server and its sign-in, wait until "
              "health_path answers, print the URL",
    "destroy": "remove the server, its sign-in, its image, and its service account; keep the "
               "storage and the logs",
}

ANSWERS = {
    "google-cloud": {
        "compute": "a Cloud Run service pdt-<app>, by default 0 to 3 instances of 1 vCPU and "
                   "512 MiB, one container from the app's image",
        "lifecycle": "scales to zero after 15 minutes with no request, starts on the next one; "
                     "`pdt deploy` sends a new revision; `pdt pause` stops the service and "
                     "`pdt unpause` starts it",
        "ingress": "HTTPS at https://pdt-<app>-<project number>.<region>.run.app; Identity-Aware "
                   "Proxy in front; no request reaches the app before the sign-in",
        "identity": "the service runs as its own service account pdt-web-<app>, which may read "
                    "only its secret and its storage.read folders and write only its own "
                    "storage folder; a visitor signs in with Google, "
                    "allowed_domains and allowed_groups are IAM bindings on the service, and "
                    "the app sees X-Goog-Authenticated-User-Email; a user: or serviceAccount: "
                    "binding added by hand stays",
        "secrets": "Secret Manager secret pdt-<app>-env, mounted as PDT_ENV_JSON",
        "storage": f"gs://pdt-data-<suffix>/<app>/; each storage.read app's folder is a "
                   f"read-only Cloud Storage volume at {STORAGE_READ_DIR}/<app>/, new files "
                   "visible within about a minute",
        "logs": "Cloud Logging; `pdt logs` reads what the deployed service wrote since its "
                "last deploy: its output and Cloud Run's system log; --follow tails them; IAP "
                "writes no per-sign-in entry on Cloud Run",
        "health": "a startup probe on web.health_path every 5 s for up to 90 s; `pdt health` "
                  "shows ok while the service serves, and `pdt runs` shows it as one row",
        "build": "the app's Dockerfile, or the generated one, pushed to Artifact Registry",
        "cost": "request-based billing: CPU and memory while a request runs, no charge at zero "
                "instances; IAP is free",
    },
    **{provider: {concern: "not yet available; the web profile runs on Google Cloud"
                  for concern in ("compute", "lifecycle", "ingress", "identity", "secrets",
                                  "storage", "logs", "health", "build", "cost")}
       for provider in ("aws", "azure", "windows")},
}


def auth_provider(app: dict) -> str:
    """`google` or `microsoft`."""
    return str(app["auth"].get("provider") or "")


def gate(app: dict, provider: str) -> str:
    """The sign-in gate this platform puts in front of the app."""
    return GATES.get((provider, auth_provider(app)), "")


def memory_mib(text: str) -> int | None:
    """512 for "512MiB", 2048 for "2GiB"; None for any other text."""
    for unit, size in (("MiB", 1), ("GiB", 1024)):
        number = text[:-len(unit)] if isinstance(text, str) and text.endswith(unit) else ""
        if number.isdigit():
            return int(number) * size
    return None


def resources(app: dict) -> dict:
    """web.resources with RESOURCE_DEFAULTS filled in."""
    return {**RESOURCE_DEFAULTS, **(app["web"].get("resources") or {})}


def resource_problems(value, where: str) -> list[str]:
    from pdt import config

    if value is None:
        return []
    if not isinstance(value, dict):
        return [f"{where}: web.resources must be a mapping"]
    problems = config.key_problems(f"{where}: web.resources", value, set(RESOURCE_DEFAULTS))
    merged = {**RESOURCE_DEFAULTS, **value}

    def whole(key: str, low: int, high: int) -> bool:
        number = merged[key]
        if isinstance(number, bool) or not isinstance(number, int) or not low <= number <= high:
            problems.append(f"{where}: web.resources.{key} must be a whole number from {low} "
                            f"to {high}")
            return False
        return True

    cpu = merged["cpu"]
    cpu_ok = cpu in CPU_MEMORY and not isinstance(cpu, bool)
    if not cpu_ok:
        problems.append(f"{where}: web.resources.cpu must be one of "
                        + ", ".join(str(count) for count in CPU_MEMORY))
    memory = memory_mib(merged["memory"])
    if memory is None:
        problems.append(f"{where}: web.resources.memory must be a size such as 512MiB or 2GiB")
    elif cpu_ok and not CPU_MEMORY[cpu][0] <= memory <= CPU_MEMORY[cpu][1]:
        low, high = CPU_MEMORY[cpu]
        problems.append(f"{where}: web.resources.memory: {cpu} vCPU needs from "
                        f"{low if low < 1024 else low // 1024}{'MiB' if low < 1024 else 'GiB'} "
                        f"to {high // 1024}GiB")
    whole("timeout", 1, TIMEOUT_MAX)
    whole("concurrency", 1, CONCURRENCY_MAX)
    if whole("max_instances", 1, 1000) & whole("min_instances", 0, 1000):
        if merged["min_instances"] > merged["max_instances"]:
            problems.append(f"{where}: web.resources.min_instances must not be more than "
                            "max_instances")
    return problems


def port(app: dict) -> int:
    return int(app["web"].get("port", DEFAULT_PORT))


def health_path(app: dict) -> str:
    return str(app["web"].get("health_path", DEFAULT_HEALTH_PATH))


def _names(value, what: str, where: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        return [f"{where}: {what} must be a list of names"]
    return []


def problems(app: dict, provider: str) -> list[str]:
    from pdt import config

    name = app["name"]
    where = f"{name}/{config.APP_FILE}"
    problems = []
    if provider in config.PROVIDERS and provider not in PROVIDERS:
        problems.append(f"{name}: app_profile: web is not yet available on the {provider} "
                        f"provider; it runs on {', '.join(PROVIDERS)}")
    if app["schedule"] is not None:
        problems.append(f"{name}: schedule: a web app runs all the time; remove the key")
    web, auth = app["web"], app["auth"]
    problems.extend(config.key_problems(f"{where}: web", web, WEB_KEYS))
    problems.extend(config.key_problems(f"{where}: web.auth", auth, AUTH_KEYS))
    port = web.get("port", DEFAULT_PORT)
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        problems.append(f"{where}: web.port must be a number from 1 to 65535")
    path = web.get("health_path", DEFAULT_HEALTH_PATH)
    if not isinstance(path, str) or not path.startswith("/"):
        problems.append(f"{where}: web.health_path must be a path that starts with /")
    chosen = auth.get("provider")
    if chosen not in AUTH_PROVIDERS:
        problems.append(f"{where}: web.auth.provider must be {' or '.join(AUTH_PROVIDERS)}")
    elif provider in PROVIDERS and (provider, chosen) not in GATES:
        problems.append(f"{where}: web.auth.provider: {chosen} sign-in is not yet available on the "
                        f"{provider} provider; it works with "
                        + ", ".join(sorted(p for (prov, p) in GATES if prov == provider)))
    problems.extend(_names(auth.get("allowed_domains"), "allowed_domains", f"{where}: web.auth"))
    problems.extend(_names(auth.get("allowed_groups"), "allowed_groups", f"{where}: web.auth"))
    if not auth.get("allowed_domains") and not auth.get("allowed_groups"):
        problems.append(f"{where}: web.auth: list who may enter in allowed_domains, "
                        "allowed_groups, or both")
    problems.extend(resource_problems(web.get("resources"), where))
    return problems
