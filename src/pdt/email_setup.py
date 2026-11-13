"""The guided Microsoft 365 setup of `pdt alerts setup`.

`microsoft_365` gets a Microsoft Graph token from the Azure CLI sign-in on this
computer (`pdt az`), and starts that sign-in when there is none. `ensure` then
makes sure of three items in the signed-in organization, and prints one line for
each: the single-tenant app registration REGISTRATION with its service
principal, a certificate on it whose private key is the file CERTIFICATE in the
project (a .pfx with no password, in a folder that git ignores), and the
Microsoft Graph application permission PERMISSION, granted in the organization.
It reads the state first and changes only what is not yet true, so a second run
changes nothing. It returns the three env vars that the `microsoft-365` method
of pdt.email_alert reads.

When the account cannot make a change (HTTP 403), `ensure` says which role is
missing, prints the admin consent link when the registration exists, keeps what
it made, and returns None. The next run continues from there.

`ensure` takes `call(method, path, body)` for every Microsoft Graph request, so
a test gives the answers. `send_test` sends the test message; right after this
process gave the permission, Microsoft can refuse the registration for a short
time, so it waits and tries again for at most GRANT_WAIT_SECONDS.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from pdt import alert_message, console, email_alert

GRAPH = "https://graph.microsoft.com/v1.0"
GRAPH_APP_ID = "00000003-0000-0000-c000-000000000000"
REGISTRATION = "pdt-email"
CERTIFICATE = ".secrets/pdt-email.pfx"
CERTIFICATE_DAYS = 3650
PERMISSION = "Mail.Send"
# The admin consent page sends the browser to an address that the registration lists.
REDIRECT_URI = "http://localhost"
GRANT_WAIT_SECONDS = 120
RETRY_SECONDS = 10
# A new registration is not at once known to every Microsoft server.
NEW_REGISTRATION_TRIES = 6
# What a refusal of a registration, certificate, or permission that is too new holds.
TOO_NEW = ("HTTP 401", "HTTP 403", "AADSTS")
# True once this process gave the permission.
granted_now = False


class GraphError(Exception):
    def __init__(self, status: int, text: str):
        super().__init__(text)
        self.status = status


class SetupError(Exception):
    pass


def az(*args: str) -> subprocess.CompletedProcess:
    """Run one Azure CLI command the way `pdt az` does, and return what it printed."""
    from pdt import deploy
    script = Path(__file__).with_name("deploy_azure.py")
    deploy.announce_install(script)
    return subprocess.run(["uv", "run", "--project", str(Path.cwd()), "--script", str(script),
                           "az", *args], capture_output=True, text=True)


def az_token() -> str:
    done = az("account", "get-access-token", "--resource-type", "ms-graph", "--query",
              "accessToken", "--output", "tsv")
    if done.returncode != 0 or done.stdout.strip() == "":
        return ""
    return done.stdout.strip().splitlines()[-1]


def claims(token: str) -> dict:
    try:
        payload = token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError):
        return {}


def graph_call(token: str, method: str, path: str, body: dict | None = None) -> dict:
    request = urllib.request.Request(
        GRAPH + urllib.parse.quote(path, safe="/?=&$(),'"), method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=alert_message.TIMEOUT_SECONDS) as response:
            text = response.read().decode()
    except urllib.error.HTTPError as e:
        text = e.read().decode(errors="replace")
        try:
            detail = json.loads(text)["error"]
            text = f"{detail['code']}: {detail['message']}"
        except (ValueError, KeyError, TypeError):
            pass
        raise GraphError(e.code, f"Microsoft Graph answered HTTP {e.code}: {text[:300]}") from None
    except urllib.error.URLError as e:
        raise SetupError(f"Microsoft Graph could not be reached: {e.reason}") from None
    return json.loads(text) if text else {}


def sign_in() -> tuple[str, str, Callable]:
    """The tenant id and the user of the Azure CLI sign-in, and `call` for Microsoft Graph."""
    with console.waiting("checking: Microsoft sign-in", once=True):
        token = az_token()
    if token == "":
        console.say("pdt opens the Microsoft sign-in in your browser. Sign in as an "
                    "administrator.")
        with console.waiting("waiting: Microsoft sign-in in the browser"):
            done = az("login", "--allow-no-subscriptions")
        if done.returncode != 0:
            raise SetupError((done.stdout + done.stderr).strip() or "the sign-in did not finish")
        token = az_token()
        if token == "":
            raise SetupError("the Microsoft sign-in gave no access to Microsoft Graph")
    found = claims(token)
    user = str(found.get("upn") or found.get("unique_name") or "your account")
    console.say(f"Signed in to Microsoft as {console.value(user)}.")
    return (str(found.get("tid") or ""), user,
            lambda method, path, body=None: graph_call(token, method, path, body))


def thumbprints(key_credentials: list[dict]) -> set[str]:
    found = set()
    for credential in key_credentials:
        if credential.get("key"):
            found.add(hashlib.sha1(base64.b64decode(credential["key"])).hexdigest().upper())
        elif credential.get("customKeyIdentifier"):
            found.add(str(credential["customKeyIdentifier"]).upper())
    return found


def new_certificate() -> tuple[bytes, bytes]:
    """A new key and self-signed certificate: the .pfx with no password, and the
    certificate alone (DER)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives.serialization import pkcs12
    from cryptography.x509.oid import NameOID
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, REGISTRATION)])
    now = datetime.now(timezone.utc)
    certificate = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
                   .public_key(key.public_key()).serial_number(x509.random_serial_number())
                   .not_valid_before(now).not_valid_after(now + timedelta(days=CERTIFICATE_DAYS))
                   .sign(key, hashes.SHA256()))
    pfx = pkcs12.serialize_key_and_certificates(REGISTRATION.encode(), key, certificate, None,
                                                serialization.NoEncryption())
    return pfx, certificate.public_bytes(serialization.Encoding.DER)


def saved_certificate(path: Path) -> bytes:
    """The certificate (DER) of the .pfx file at `path`."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.serialization.pkcs12 import load_key_and_certificates
    try:
        key, certificate, _extra = load_key_and_certificates(path.read_bytes(), None)
    except ValueError as e:
        raise SetupError(f"{path} could not be loaded as a .pfx file with no password: {e}") from None
    if key is None or certificate is None:
        raise SetupError(f"{path} holds no private key or no certificate")
    return certificate.public_bytes(serialization.Encoding.DER)


def write_private(path: Path, data: bytes) -> None:
    """Write the key file readable by its owner alone, in a folder that git ignores."""
    path.parent.mkdir(parents=True, exist_ok=True)
    ignore = path.parent / ".gitignore"
    if not ignore.exists():
        ignore.write_text("*\n")
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as f:
        f.write(data)


def ensure(project: Path, tenant_id: str, user: str, call: Callable) -> dict[str, str] | None:
    global granted_now
    needs = ("create an app registration", "Application Developer or Global Administrator")
    registration = None
    try:
        found = []
        client_id = alert_message.secret_value("PDT_EMAIL_CLIENT_ID")
        if client_id:
            found = call("GET", f"/applications?$filter=appId eq '{client_id}'&$select=id")["value"]
        if not found:
            found = call("GET", f"/applications?$filter=displayName eq '{REGISTRATION}'"
                                "&$select=id")["value"]
        if found:
            registration = call("GET", f"/applications/{found[0]['id']}?$select=id,appId,"
                                       "keyCredentials,requiredResourceAccess,web")
        else:
            registration = call("POST", "/applications", {
                "displayName": REGISTRATION, "signInAudience": "AzureADMyOrg",
                "web": {"redirectUris": [REDIRECT_URI]}})
        app_id = registration["appId"]
        principal = None
        for attempt in range(NEW_REGISTRATION_TRIES):
            try:
                principal = call("GET", f"/servicePrincipals(appId='{app_id}')?$select=id")
                break
            except GraphError as e:
                if e.status != 404:
                    raise
            try:
                principal = call("POST", "/servicePrincipals", {"appId": app_id})
                break
            except GraphError as e:
                if e.status not in (400, 404) or attempt == NEW_REGISTRATION_TRIES - 1:
                    raise
            time.sleep(RETRY_SECONDS / 2)
        if found:
            console.done(f"the app registration {console.value(REGISTRATION)} is already there")
        else:
            console.done(f"created the app registration {console.value(REGISTRATION)}")

        needs = ("change the app registration",
                 "Application Administrator or Global Administrator, or ownership of the "
                 "registration")
        path = project / CERTIFICATE
        created = not path.is_file()
        if created:
            pfx, certificate = new_certificate()
            write_private(path, pfx)
        else:
            certificate = saved_certificate(path)
        thumbprint = hashlib.sha1(certificate).hexdigest().upper()
        if thumbprint in thumbprints(registration.get("keyCredentials") or []):
            console.done(f"the certificate {console.value(CERTIFICATE)} is already there")
        else:
            # A PATCH replaces the whole list, so the body repeats each certificate it has.
            call("PATCH", f"/applications/{registration['id']}", {"keyCredentials": [
                *(registration.get("keyCredentials") or []),
                {"type": "AsymmetricX509Cert", "usage": "Verify",
                 "displayName": f"CN={REGISTRATION}",
                 "key": base64.b64encode(certificate).decode()}]})
            if created:
                console.done(f"created the certificate {console.value(CERTIFICATE)}")
            else:
                console.done(f"added the certificate {console.value(CERTIFICATE)} to {console.value(REGISTRATION)}")

        graph = call("GET", f"/servicePrincipals(appId='{GRAPH_APP_ID}')?$select=id,appRoles")
        role_id = next(role["id"] for role in graph["appRoles"] if role["value"] == PERMISSION
                       and "Application" in role["allowedMemberTypes"])
        access = registration.get("requiredResourceAccess") or []
        if not any(entry["resourceAppId"] == GRAPH_APP_ID and any(
                item["id"] == role_id and item["type"] == "Role"
                for item in entry["resourceAccess"]) for entry in access):
            others = [entry for entry in access if entry["resourceAppId"] != GRAPH_APP_ID]
            listed = [item for entry in access if entry["resourceAppId"] == GRAPH_APP_ID
                      for item in entry["resourceAccess"]]
            call("PATCH", f"/applications/{registration['id']}", {"requiredResourceAccess": [
                *others, {"resourceAppId": GRAPH_APP_ID,
                          "resourceAccess": [*listed, {"id": role_id, "type": "Role"}]}]})
        needs = (f"give {REGISTRATION} the permission {PERMISSION}",
                 "Privileged Role Administrator or Global Administrator")
        assignments = call("GET", f"/servicePrincipals/{principal['id']}/appRoleAssignments")
        if any(item["resourceId"] == graph["id"] and item["appRoleId"] == role_id
               for item in assignments["value"]):
            console.done(f"{console.value(REGISTRATION)} already has the permission {console.value(PERMISSION)}")
        else:
            call("POST", f"/servicePrincipals/{graph['id']}/appRoleAssignedTo", {
                "principalId": principal["id"], "resourceId": graph["id"], "appRoleId": role_id})
            granted_now = True
            console.done(f"gave {console.value(REGISTRATION)} the permission {console.value(PERMISSION)}")
    except GraphError as e:
        if e.status != 403:
            raise SetupError(str(e)) from None
        console.failed(f"{console.value(user)} cannot {console.escape(needs[0])} in this "
                       "Microsoft 365 organization.")
        console.say(f"That needs one of these roles: {console.escape(needs[1])}.")
        steps = [("pdt az login --allow-no-subscriptions",
                  "sign in with an account that has the role")]
        if registration is not None and needs[0].startswith("give"):
            link = (f"https://login.microsoftonline.com/{tenant_id}/adminconsent"
                    f"?client_id={registration['appId']}"
                    f"&redirect_uri={urllib.parse.quote(REDIRECT_URI, safe='')}")
            console.say("Or an administrator opens this link and approves the permission. The "
                        "browser then shows a page that does not load, which is correct:")
            console.command(link)
            steps = []
        console.next_steps([*steps, ("pdt alerts setup", "run the setup again after that")],
                           "Then:")
        return None
    return {"PDT_EMAIL_TENANT_ID": tenant_id, "PDT_EMAIL_CLIENT_ID": app_id,
            "PDT_EMAIL_PRIVATE_KEY_PATH": CERTIFICATE}


def microsoft_365(project: Path) -> dict[str, str] | None:
    """The env vars of the microsoft-365 method after the guided setup, or None when the
    setup did not finish."""
    try:
        tenant_id, user, call = sign_in()
        return ensure(project, tenant_id, user, call)
    except SetupError as e:
        console.failed(f"Microsoft 365 is not set up: {console.escape(str(e))}")
        return None


def send_test(entry: dict, project: str) -> None:
    deadline = time.monotonic() + GRANT_WAIT_SECONDS
    while True:
        try:
            email_alert.send_ready(entry, project)
            return
        except RuntimeError as e:
            too_new = (granted_now and entry["method"] == "microsoft-365"
                       and any(mark in str(e) for mark in TOO_NEW))
            if not too_new or time.monotonic() >= deadline:
                raise
        with console.waiting("waiting: Microsoft 365 to accept the new permission"):
            time.sleep(RETRY_SECONDS)
