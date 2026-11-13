"""The `web` profile: an HTTP server that runs all the time behind the platform's sign-in.

The app is any container that listens on the port in `web.port` (PDT_PORT), or a
`run.py` that does. It needs no sign-in code. The platform's own gate stands in
front of it and lets no request through until the visitor signed in:
Identity-Aware Proxy on Cloud Run, oauth2-proxy on a Windows PC. `web.auth.provider`
names where visitors sign in (`google`; `microsoft` has no gate yet);
`web.auth: public` serves everyone. Each gate sets a
header with the visitor's email (`pdt.utils.auth.current_user` reads all of them),
and the app may ignore it.

`web.auth.allowed_domains` and `web.auth.allowed_groups` narrow who may enter. Each
platform enforces what it can (see ANSWERS); where it cannot, deploy says so,
and the app checks the header itself.

`storage.read` names the apps whose files this app may read. The deployed app
finds each one's folder read-only under STORAGE_READ_DIR/<app>/, the way the
platform does it best (a Cloud Storage volume on Cloud Run), and the folder URLs
in PDT_STORAGE_READ_URLS.
"""

from __future__ import annotations

NAME = "web"
DESCRIPTION = "an HTTP server that runs all the time behind the platform's sign-in"

KEYS = {"web"}
WEB_KEYS = {"port", "health_path", "hostname", "tls_cert", "tls_key", "auth"}
AUTH_KEYS = {"provider", "allowed_domains", "allowed_groups"}
PUBLIC = "public"
AUTH_PROVIDERS = ("google", "microsoft")
DEFAULT_PORT = 8080
DEFAULT_HEALTH_PATH = "/"
# The port the Windows proxy listens on. There the app's own port is reachable from 127.0.0.1 only.
PROXY_PORT = 443
# The providers that can run this profile today. The others are refused at validation
# time, so a deploy never gets as far as the cloud.
PROVIDERS = ("google-cloud", "windows")
# Which sign-in gate each (provider, web.auth.provider) pair gets. A pair that is not here is
# refused at validation time.
GATES = {
    ("google-cloud", "google"): "Identity-Aware Proxy",
    ("windows", "google"): "oauth2-proxy",
}

# The env vars oauth2-proxy needs on Windows, where no platform makes the OAuth client.
ENV_CLIENT_ID = "PDT_AUTH_CLIENT_ID"
ENV_CLIENT_SECRET = "PDT_AUTH_CLIENT_SECRET"
# Windows only: oauth2-proxy's cookie secret, and, with allowed_groups, the service account
# with domain-wide delegation that reads Google group membership.
ENV_COOKIE_SECRET = "PDT_AUTH_COOKIE_SECRET"
ENV_SERVICE_ACCOUNT_PATH = "PDT_AUTH_GOOGLE_SERVICE_ACCOUNT_PATH"
ENV_ADMIN_EMAIL = "PDT_AUTH_GOOGLE_ADMIN_EMAIL"
# What the app reads.
ENV_PORT = "PDT_PORT"
ENV_URL = "PDT_URL"
ENV_STORAGE_READ_DIR = "PDT_STORAGE_READ_DIR"
# Where the deployed app finds a read-only copy of each `storage.read` app's files.
STORAGE_READ_DIR = "/pdt/storage"

COMMANDS = {
    "run": "start the app on this computer with no sign-in, from run.py or its Dockerfile; "
           "Ctrl+C stops",
    "run --deployed": "not for a web app; it runs all the time, and `pdt deploy` restarts it "
                      "with the deployed code",
    "runs": "one row per start of the server, with its exit code when it stopped",
    "logs": "the server's log; the running server by default",
    "health": "whether the deployed server answers web.health_path",
    "pause": "stop the server; `unpause` starts it again",
    "deploy": "build the app, create or update the server and its sign-in, wait until "
              "health_path answers, print the URL",
    "destroy": "remove the server, its sign-in, and the logs; keep the storage",
}

ANSWERS = {
    "google-cloud": {
        "compute": "a Cloud Run service pdt-<app>, 0 to 3 instances of 1 vCPU and 512 MiB, one "
                   "container from the app's image",
        "lifecycle": "scales to zero after 15 minutes with no request, starts on the next one; "
                     "`pdt deploy` sends a new revision; `pdt pause` sets the ingress to internal "
                     "so no visitor reaches it",
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
    "windows": {
        "compute": "two Windows services on this PC, pdt-<app> (the app, through WinSW) and "
                   "pdt-<app>-proxy (oauth2-proxy, through WinSW)",
        "lifecycle": "both services start at boot and restart after a failure; no time limit; "
                     "`pdt deploy` restarts them so they run the deployed code",
        "ingress": f"the proxy listens on TCP {PROXY_PORT} with the certificate in web.tls_cert; "
                   "a firewall rule pdt-<app> opens the port; the app listens on 127.0.0.1 only",
        "identity": "each service runs as its virtual account NT SERVICE\\pdt-<app>; a visitor "
                    "signs in with Google, the proxy enforces allowed_domains and "
                    "allowed_groups, and the app sees X-Forwarded-Email",
        "secrets": "the project's .env file, read when each service starts",
        "storage": "%ProgramData%\\pdt\\<app>\\storage; storage.read grants the service read "
                   "rights on each named app's storage folder",
        "logs": "%ProgramData%\\pdt\\<app>\\logs, one file per start of the server; the proxy "
                "logs each request with the visitor's email",
        "health": "an HTTP request to web.health_path; the service state from Get-Service",
        "build": "none; uv runs run.py from the project folder",
        "cost": "none",
    },
    **{provider: {concern: "not yet available; the web profile runs on Google Cloud and Windows"
                  for concern in ("compute", "lifecycle", "ingress", "identity", "secrets",
                                  "storage", "logs", "health", "build", "cost")}
       for provider in ("aws", "azure")},
}


def auth_provider(app: dict) -> str:
    """`google`, `microsoft`, or PUBLIC."""
    return str(app["auth"].get("provider") or "")


def is_public(app: dict) -> bool:
    return auth_provider(app) == PUBLIC


def gate(app: dict, provider: str) -> str:
    """The sign-in gate this platform puts in front of the app, or "" for a public app."""
    if is_public(app):
        return ""
    return GATES.get((provider, auth_provider(app)), "")


def required_env(app: dict) -> list[str]:
    """The env vars the sign-in gate needs on this app's provider."""
    provider = app["platform"].get("provider", "")
    if is_public(app):
        return []
    if provider == "windows":
        names = [ENV_CLIENT_ID, ENV_CLIENT_SECRET, ENV_COOKIE_SECRET]
        if app["auth"].get("allowed_groups"):
            names += [ENV_SERVICE_ACCOUNT_PATH, ENV_ADMIN_EMAIL]
        return names
    return []


def port(app: dict) -> int:
    return int(app["web"].get("port", DEFAULT_PORT))


def health_path(app: dict) -> str:
    return str(app["web"].get("health_path", DEFAULT_HEALTH_PATH))


def url(app: dict) -> str:
    """The URL visitors use when `web.hostname` is set (Windows), else "": a cloud
    provider prints the URL its platform gave the app when it deploys."""
    hostname = app["web"].get("hostname")
    if not hostname:
        return ""
    return f"https://{hostname}/"


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
    for key in ("hostname", "tls_cert", "tls_key"):
        if key in web and (not isinstance(web[key], str) or web[key] == ""):
            problems.append(f"{where}: web.{key} must be text")
    if provider == "windows":
        missing = [key for key in ("hostname", "tls_cert", "tls_key") if not web.get(key)]
        if missing:
            problems.append(
                f"{where}: web: on Windows a web app needs {', '.join(missing)}: the DNS name "
                "visitors use and the PEM certificate and key files for it, because the PC has "
                "no platform certificate")
    chosen = auth.get("provider")
    if not auth:
        problems.append(f"{where}: web.auth: say who may use the app: web.auth: public serves "
                        f"everyone, and web.auth.provider: {' or '.join(AUTH_PROVIDERS)} puts a "
                        "sign-in in front of it")
    elif chosen not in (PUBLIC, *AUTH_PROVIDERS):
        problems.append(f"{where}: web.auth.provider must be {PUBLIC}, "
                        f"{', or '.join(AUTH_PROVIDERS)}")
    elif chosen == PUBLIC:
        for key in ("allowed_domains", "allowed_groups"):
            if auth.get(key):
                problems.append(f"{where}: web.auth.{key}: a public app has no sign-in, so it cannot "
                                "limit who enters; set web.auth.provider instead")
    elif provider in PROVIDERS and (provider, chosen) not in GATES:
        problems.append(f"{where}: web.auth.provider: {chosen} sign-in is not yet available on the "
                        f"{provider} provider; it works with "
                        + ", ".join(sorted(p for (prov, p) in GATES if prov == provider)))
    problems.extend(_names(auth.get("allowed_domains"), "allowed_domains", f"{where}: web.auth"))
    problems.extend(_names(auth.get("allowed_groups"), "allowed_groups", f"{where}: web.auth"))
    return problems
