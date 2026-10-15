"""Create or update the Entra app registration an app signs in to Microsoft Graph as.

`pdt entra <app>` runs this inside deploy_azure.py's process, where the Azure
CLI is installed. The app's `entra: graph_permissions:` lists the Microsoft
Graph application permissions it needs.

A person who may grant those permissions signs in with `az login` into a
temporary Azure CLI folder, so the session never mixes with the login that
deploy uses, and it is gone when the command ends. Everything after the
sign-in is a Microsoft Graph call made with that session's token.

When PDT_AZURE_CLIENT_ID is set, that app registration gets the permissions
it lacks. Otherwise pdt creates `pdt-<app>`, or reuses an app registration of
that name. Each permission is added to the app's API permissions and granted
(admin consent) as an app role assignment of its service principal; `az ad app
permission admin-consent` is not used, since it can return success and grant
nothing. When no credential is set, pdt adds a client secret and saves
PDT_AZURE_TENANT_ID, PDT_AZURE_CLIENT_ID, and PDT_AZURE_CLIENT_SECRET in the
app's own .env file. Last, it signs in as the app until the token carries
every permission, since Entra takes a while to hand out new grants.
"""

from __future__ import annotations

import base64
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pdt import console

AZ = [sys.executable, "-m", "azure.cli"]
GRAPH = "https://graph.microsoft.com/v1.0"
GRAPH_APP_ID = "00000003-0000-0000-c000-000000000000"
TENANT_ENV = "PDT_AZURE_TENANT_ID"
CLIENT_ENV = "PDT_AZURE_CLIENT_ID"
SECRET_ENV = "PDT_AZURE_CLIENT_SECRET"
CERT_ENVS = ("PDT_AZURE_PRIVATE_KEY_B64", "PDT_AZURE_PRIVATE_KEY_PATH")
SECRET_DAYS = 365
# A new service principal or grant can take a while to reach every Entra server.
RETRY_WAITS = (5, 10, 20, 30, 0)
VERIFY_SECONDS = 300
VERIFY_WAIT = 15


class EntraError(Exception):
    pass


class Graph:
    """Microsoft Graph calls with one access token."""

    def __init__(self, token: str):
        self.token = token

    def call(self, method: str, path: str, body: dict | None = None) -> dict:
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(
            GRAPH + path, data=data, method=method,
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raise EntraError(f"Microsoft Graph {method} {path.split('?')[0]} returned "
                             f"{exc.code}: {graph_message(exc.read())}") from None
        return json.loads(raw) if raw else {}

    def get(self, path: str) -> dict:
        return self.call("GET", path)

    def post(self, path: str, body: dict) -> dict:
        return self.call("POST", path, body)

    def patch(self, path: str, body: dict) -> dict:
        return self.call("PATCH", path, body)


def graph_message(raw: bytes) -> str:
    try:
        return json.loads(raw)["error"]["message"]
    except (ValueError, KeyError, TypeError):
        return raw.decode(errors="replace")[:300]


def query(filter_: str, select: str) -> str:
    return "?" + urllib.parse.urlencode({"$filter": filter_, "$select": select}, safe="$,'")


@dataclass
class Plan:
    tenant: str
    name: str
    wanted: list[str]
    graph_sp: str
    role_ids: dict[str, str]
    app: dict | None
    sp: dict | None
    to_list: list[str] = field(default_factory=list)
    to_grant: list[str] = field(default_factory=list)
    new_secret: bool = False


def roles_of_graph(graph: Graph) -> tuple[str, dict[str, str]]:
    """Microsoft Graph's service principal id and its application permissions by name."""
    found = graph.get("/servicePrincipals" + query(f"appId eq '{GRAPH_APP_ID}'", "id,appRoles"))
    if not found.get("value"):
        raise EntraError("the tenant has no Microsoft Graph service principal")
    sp = found["value"][0]
    return sp["id"], {role["value"]: role["id"] for role in sp["appRoles"]
                      if "Application" in role.get("allowedMemberTypes", [])}


def find_app(graph: Graph, tenant: str, client_id: str, name: str) -> dict | None:
    if client_id:
        filter_ = f"appId eq '{client_id}'"
    else:
        filter_ = f"displayName eq '{name}'"
    apps = graph.get("/applications" + query(
        filter_, "id,appId,displayName,requiredResourceAccess"))["value"]
    if client_id and not apps:
        raise EntraError(f"{CLIENT_ENV} is {client_id}, but tenant {tenant} has no app "
                         "registration with that client id. Sign in to the tenant the app "
                         f"belongs to, or remove {CLIENT_ENV} to create a new app.")
    if len(apps) > 1:
        raise EntraError(f"tenant {tenant} has {len(apps)} app registrations named {name}. "
                         f"Put the client id of the one to use in {CLIENT_ENV}.")
    return apps[0] if apps else None


def find_sp(graph: Graph, app_id: str) -> dict | None:
    found = graph.get("/servicePrincipals" + query(f"appId eq '{app_id}'", "id"))["value"]
    return found[0] if found else None


def listed_roles(app: dict) -> set[str]:
    return {access["id"] for resource in app.get("requiredResourceAccess") or []
            if resource["resourceAppId"] == GRAPH_APP_ID
            for access in resource["resourceAccess"] if access["type"] == "Role"}


def granted_roles(graph: Graph, sp_id: str, graph_sp: str) -> set[str]:
    assignments = graph.get(f"/servicePrincipals/{sp_id}/appRoleAssignments")["value"]
    return {a["appRoleId"] for a in assignments if a["resourceId"] == graph_sp}


def has_credential(env) -> bool:
    return any(str(env.get(name, "")).strip() for name in (SECRET_ENV, *CERT_ENVS))


def make_plan(graph: Graph, tenant: str, name: str, wanted: list[str], client_id: str,
              env) -> Plan:
    graph_sp, role_ids = roles_of_graph(graph)
    unknown = [permission for permission in wanted if permission not in role_ids]
    if unknown:
        raise EntraError("these are not Microsoft Graph application permissions: "
                         f"{', '.join(unknown)}. Check the spelling in entra: graph_permissions:.")
    app = find_app(graph, tenant, client_id, name)
    sp = find_sp(graph, app["appId"]) if app else None
    listed = listed_roles(app) if app else set()
    granted = granted_roles(graph, sp["id"], graph_sp) if sp else set()
    return Plan(tenant, app["displayName"] if app else name, wanted, graph_sp, role_ids, app, sp,
                [p for p in wanted if role_ids[p] not in listed],
                [p for p in wanted if role_ids[p] not in granted],
                app is None or not client_id or not has_credential(env))


def show_plan(plan: Plan, user: str, env_file: Path) -> None:
    console.field("Tenant", plan.tenant)
    console.field("Signed in as", user)
    if plan.app is None:
        console.field("App registration", f"{plan.name} (new)")
    else:
        console.field("App registration", f"{plan.name} ({plan.app['appId']})")
    if plan.to_grant or plan.to_list:
        console.say("Microsoft Graph application permissions to add and grant:")
        for permission in plan.wanted:
            if permission in plan.to_grant or permission in plan.to_list:
                console.bullet(console.value(permission))
    if plan.new_secret:
        console.say(f"A new client secret, valid {SECRET_DAYS} days, goes in "
                    f"{console.value(str(env_file))} with {console.value(TENANT_ENV)} and "
                    f"{console.value(CLIENT_ENV)}.")


def retried(action, what: str):
    """Run action, retrying while Entra has not yet seen an object pdt just created."""
    for wait in RETRY_WAITS:
        try:
            return action()
        except EntraError as exc:
            if wait == 0 or not any(code in str(exc) for code in ("returned 400", "returned 404")):
                raise
            console.bullet(f"Entra has not caught up with the new {what}; retrying in {wait}s...",
                           indent=4)
            time.sleep(wait)


def apply(graph: Graph, plan: Plan, app_name: str) -> tuple[str, str]:
    """Make the changes the plan lists. Returns the client id and a new secret, or ""."""
    access = [{"id": plan.role_ids[p], "type": "Role"} for p in plan.wanted]
    if plan.app is None:
        app = graph.post("/applications", {
            "displayName": plan.name, "signInAudience": "AzureADMyOrg",
            "notes": f"pdt created this app registration for the {app_name} app.",
            "requiredResourceAccess": [{"resourceAppId": GRAPH_APP_ID, "resourceAccess": access}]})
        console.done(f"Created app registration {console.value(plan.name)} "
                     f"({console.value(app['appId'])}).")
    else:
        app = plan.app
        if plan.to_list:
            resources = [r for r in app.get("requiredResourceAccess") or []
                         if r["resourceAppId"] != GRAPH_APP_ID]
            kept = [a for r in app.get("requiredResourceAccess") or []
                    if r["resourceAppId"] == GRAPH_APP_ID for a in r["resourceAccess"]]
            added = [a for a in access if a["id"] not in {k["id"] for k in kept if k["type"] == "Role"}]
            resources.append({"resourceAppId": GRAPH_APP_ID, "resourceAccess": kept + added})
            graph.patch(f"/applications/{app['id']}", {"requiredResourceAccess": resources})
            console.done(f"Added {len(plan.to_list)} permission(s) to the app's API permissions.")
    sp = plan.sp or retried(lambda: graph.post("/servicePrincipals", {"appId": app["appId"]}),
                            "app registration")
    for permission in plan.to_grant:
        retried(lambda permission=permission: graph.post(
            f"/servicePrincipals/{sp['id']}/appRoleAssignments",
            {"principalId": sp["id"], "resourceId": plan.graph_sp,
             "appRoleId": plan.role_ids[permission]}), "service principal")
    if plan.to_grant:
        console.done(f"Granted {len(plan.to_grant)} permission(s) (admin consent).")
    secret = ""
    if plan.new_secret:
        end = datetime.now(timezone.utc) + timedelta(days=SECRET_DAYS)
        created = graph.post(f"/applications/{app['id']}/addPassword", {"passwordCredential": {
            "displayName": f"pdt {app_name}", "endDateTime": end.strftime("%Y-%m-%dT%H:%M:%SZ")}})
        secret = created["secretText"]
        console.done(f"Created a client secret that expires {console.value(f'{end:%Y-%m-%d}')}.")
    return app["appId"], secret


def save_env(env_file: Path, values: dict[str, str]) -> None:
    from dotenv import set_key

    from pdt.utils.env_secret import private_file
    env_file.parent.mkdir(parents=True, exist_ok=True)
    private_file(env_file)
    for name, value in values.items():
        set_key(str(env_file), name, value, quote_mode="never")
        os.environ[name] = value
    names = ", ".join(map(console.value, values))
    console.done(f"Saved {names} in {console.value(str(env_file))}.")


def token_roles(tenant: str, client_id: str, secret: str) -> set[str] | None:
    """The app permissions in a token the app gets with its secret; None while Entra
    does not accept the secret yet."""
    body = urllib.parse.urlencode({
        "client_id": client_id, "client_secret": secret, "grant_type": "client_credentials",
        "scope": "https://graph.microsoft.com/.default"}).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(
                f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token", data=body),
                timeout=60) as response:
            token = json.loads(response.read())["access_token"]
    except urllib.error.HTTPError:
        return None
    payload = token.split(".")[1]
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    return set(claims.get("roles") or [])


def verify(tenant: str, client_id: str, secret: str, wanted: list[str]) -> list[str]:
    """Wait until the app's token carries every wanted permission. Returns those still missing."""
    console.status("Signing in as the app until its token carries every permission...")
    deadline = time.monotonic() + VERIFY_SECONDS
    missing = list(wanted)
    while True:
        roles = token_roles(tenant, client_id, secret)
        if roles is not None:
            missing = [p for p in wanted if p not in roles]
            if not missing:
                console.done("The app's token carries every permission.")
                return []
        if time.monotonic() >= deadline:
            return missing
        time.sleep(VERIFY_WAIT)


def run_az(config_dir: Path, *args: str, capture: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run([*AZ, *args], env={**os.environ, "AZURE_CONFIG_DIR": str(config_dir)},
                          capture_output=capture, text=True)


def sign_in(config_dir: Path, tenant: str, device_code: bool) -> tuple[str, str, str]:
    """az login into config_dir. Returns the tenant id, the user, and a Graph token."""
    args = ["login", "--allow-no-subscriptions", "--output", "none"]
    if tenant:
        args += ["--tenant", tenant]
    if device_code:
        args.append("--use-device-code")
    who = "an account" if not tenant else f"an account of tenant {console.value(tenant)}"
    console.say(f"Sign in as {who} that may grant Microsoft Graph application permissions "
                "(Global Administrator or Privileged Role Administrator).")
    if run_az(config_dir, *args, capture=False).returncode != 0:
        raise EntraError("az login failed; see the message above")
    shown = run_az(config_dir, "account", "show", "--output", "json")
    token = run_az(config_dir, "account", "get-access-token", "--resource-type", "ms-graph",
                   "--query", "accessToken", "--output", "tsv")
    if shown.returncode != 0 or token.returncode != 0:
        raise EntraError(f"az could not read the login: {(shown.stderr + token.stderr).strip()}")
    account = json.loads(shown.stdout)
    return account["tenantId"], (account.get("user") or {}).get("name", "unknown"), token.stdout.strip()


def setup(app: dict, assume_yes: bool, device_code: bool) -> int:
    wanted = list((app.get("entra") or {}).get("graph_permissions") or [])
    if not wanted:
        console.error(f"{console.value(app['name'])} lists no permissions under "
                      f"{console.value('entra: graph_permissions:')} in its config.yml.")
        return 1
    env_file = app["dir"] / ".env"
    client_id = os.environ.get(CLIENT_ENV, "").strip()
    # The admin's login is in config_dir. A stopped command (timeout, kill) raises
    # SystemExit instead of dying, so the folder is still removed.
    signal.signal(signal.SIGTERM, lambda signum, _frame: sys.exit(128 + signum))
    try:
        with tempfile.TemporaryDirectory(prefix="pdt-entra-") as config_dir:
            tenant, user, token = sign_in(Path(config_dir), os.environ.get(TENANT_ENV, "").strip(),
                                          device_code)
            graph = Graph(token)
            plan = make_plan(graph, tenant, f"pdt-{app['name']}", wanted, client_id, os.environ)
            if not (plan.app is None or plan.to_list or plan.to_grant or plan.new_secret):
                console.done(f"{console.value(plan.name)} already has every permission "
                             f"{console.value('entra: graph_permissions:')} lists.")
            else:
                show_plan(plan, user, env_file)
                if not assume_yes and not console.confirm("Make these changes?"):
                    console.say("Nothing changed.")
                    return 1
                new_client, secret = apply(graph, plan, app["name"])
                values = {}
                if os.environ.get(TENANT_ENV, "").strip() != tenant:
                    values[TENANT_ENV] = tenant
                if new_client != client_id:
                    values[CLIENT_ENV] = new_client
                if secret:
                    values[SECRET_ENV] = secret
                if values:
                    save_env(env_file, values)
    except EntraError as exc:
        console.error(console.escape(str(exc)))
        return 1
    secret = os.environ.get(SECRET_ENV, "").strip()
    if not secret:
        console.note("The app signs in with a certificate, so pdt did not check its token.")
        return 0
    missing = verify(os.environ[TENANT_ENV], os.environ[CLIENT_ENV], secret, wanted)
    if missing:
        again = console.value(f"pdt entra {app['name']}")
        lacking = ", ".join(map(console.value, missing))
        console.warn(f"After {VERIFY_SECONDS // 60} minutes the app's token still lacks "
                     f"{lacking}. Entra can take longer; run {again} again to check.")
        return 1
    return 0
