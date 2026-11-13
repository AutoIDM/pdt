"""The guided Microsoft 365 and Azure Communication Services setups of `pdt alerts setup`.

`microsoft_365` gets a Microsoft Graph token from an Azure CLI sign-in (`pdt az`)
that it keeps in AZURE_DIR, apart from the sign-in in ~/.azure that a deploy uses,
because the organization of the email can be another one than the deploy account.
When AZURE_DIR holds a sign-in, the user chooses to use it or to sign in with
another account; with none, the sign-in starts at once, in the organization of the
domain of the sender address. `mailbox` then makes sure that the sender address is
a mailbox of that organization before anything changes there. `ensure` then
makes sure of three items in the signed-in organization, and prints one line for
each: the single-tenant app registration REGISTRATION with its service
principal, a certificate on it whose private key is the file CERTIFICATE in the
project (a .pfx with no password, in a folder that git ignores), and the
Microsoft Graph application permission PERMISSION, granted in the organization.
It reads the state first and changes only what is not yet true, so a second run
changes nothing. pdt uses a registration only when the saved PDT_EMAIL_CLIENT_ID
names it or when it carries TAG, which pdt puts on each registration it creates;
another registration with the same name stops the setup. A sign-in to another
tenant than the saved PDT_EMAIL_TENANT_ID asks to replace the saved settings; with
yes, the saved PDT_EMAIL_CLIENT_ID does not count, so the registration of the new
tenant is found or created as in a first setup. An organization without a default
domain or a display name is named by the domain of the signed-in account. `keep_out_of_git` makes
sure that git ignores the certificate file. It returns the three env vars that the `microsoft-365` method
of pdt.email_alert reads.

When the account cannot make a change (HTTP 403), `ensure` says which role is
missing, prints the admin consent link when the registration exists, keeps what
it made, and returns None. The next run continues from there.

`ensure` takes `call(method, path, body)` for every Microsoft Graph request, so
a test gives the answers. `send_test` sends the test message; right after this
process gave the permission, Microsoft can refuse the registration for a short
time, so it waits and tries again for at most GRANT_WAIT_SECONDS.

`azure_communication_services` signs in with the same AZURE_DIR, takes one enabled
subscription (a menu when there are more), and talks to Azure Resource Manager with a
token of that sign-in, so no Azure CLI extension is needed. `acs_ensure` finds or creates,
in the resource group RESOURCE_GROUP, an Email Communication Service and a Communication
Service named `pdt-email-<the first 8 hex digits of the SHA-256 of the subscription id>`,
because a Communication Service name is a host name that must be unique in Azure. An empty
sender gets the Azure managed domain (sender DoNotReply@...azurecomm.net). An address gets
the verified domain of that name in the subscription and a Communication Service that
links it; pdt links it to its own Communication Service when none does, and creates the
domain, with its DNS records in an Azure DNS zone of the subscription, when there is none.
pdt changes only a resource with the tag `managed-by=pdt`, except that it links a verified
domain to its own Communication Service. The setup returns the connection string.
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

from pdt import alert_message, console, email_alert, regions
from pdt.config import data_home

AZURE_DIR = data_home() / "pdt" / "email-azure"
GRAPH ="https://graph.microsoft.com/v1.0"
GRAPH_APP_ID = "00000003-0000-0000-c000-000000000000"
REGISTRATION = "pdt-email"
# The mark on each registration that pdt creates, in the `tags` of the application.
TAG = "managed-by=pdt"
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
ARM = "https://management.azure.com"
ACS_API = "2023-04-01"
DNS_API = "2018-05-01"
GROUP_API = "2021-04-01"
RESOURCE_GROUP = "pdt-email"
TAGS = {"managed-by": "pdt"}
AZURE_DOMAIN = "AzureManagedDomain"
SENDER_USERNAME = "DoNotReply"
# The records that verify a custom domain: ownership, SPF, and the two DKIM keys.
VERIFY = ("Domain", "SPF", "DKIM", "DKIM2")
PROVISION_WAIT_SECONDS = 600
# The Azure Communication Services data location of each Azure region whose name holds the
# text; any other region (the United States, Mexico) is "United States".
DATA_LOCATIONS = (
    ("canada", "Canada"), ("brazil", "Brazil"), ("chile", "Brazil"), ("uk", "United Kingdom"),
    ("france", "France"), ("germany", "Germany"), ("norway", "Norway"),
    ("switzerland", "Switzerland"), ("uae", "United Arab Emirates"),
    ("qatar", "United Arab Emirates"), ("southafrica", "Africa"), ("australia", "Australia"),
    ("japan", "Japan"), ("korea", "Korea"), ("india", "India"), ("asia", "Asia Pacific"),
    ("newzealand", "Asia Pacific"), ("europe", "Europe"), ("sweden", "Europe"),
    ("italy", "Europe"), ("spain", "Europe"), ("poland", "Europe"), ("austria", "Europe"),
    ("belgium", "Europe"), ("denmark", "Europe"), ("israel", "Europe"))


class GraphError(Exception):
    def __init__(self, status: int, text: str):
        super().__init__(text)
        self.status = status


class SetupError(Exception):
    pass


def az(*args: str) -> subprocess.CompletedProcess:
    """Run one Azure CLI command the way `pdt az` does, with the sign-in in AZURE_DIR, and
    return what it printed."""
    from pdt import deploy
    script = Path(__file__).with_name("deploy_azure.py")
    deploy.announce_install(script)
    return subprocess.run(["uv", "run", "--project", str(Path.cwd()), "--script", str(script),
                           "az", *args], capture_output=True, text=True,
                          env={**os.environ, "AZURE_CONFIG_DIR": str(AZURE_DIR),
                               # The output is captured, so `az login` must not ask for a
                               # subscription.
                               "AZURE_CORE_LOGIN_EXPERIENCE_V2": "off"})


def account() -> dict:
    """What `az account show` says of the sign-in in AZURE_DIR, or {} with none."""
    done = az("account", "show", "--output", "json")
    if done.returncode != 0:
        return {}
    try:
        found = json.loads(done.stdout)
    except ValueError:
        return {}
    return found if isinstance(found, dict) else {}


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


def choose(labels: list[str]) -> int:
    """The index of the label the user picks, from a menu or from numbered lines."""
    if console.can_menu():
        return console.choose_one(labels)
    for number, label in enumerate(labels, 1):
        console.choice(number, label)
    while True:
        answer = console.ask("Which one?", "1") or "1"
        if answer.isdigit() and 1 <= int(answer) <= len(labels):
            return int(answer) - 1
        console.say(f"Please answer a number from 1 to {console.value(len(labels))}.")


def sign_in(sender: str) -> tuple[str, str, str, Callable]:
    """The tenant id, the user, and the organization of the Azure CLI sign-in in AZURE_DIR,
    and `call` for Microsoft Graph. A new sign-in goes to the organization of the domain of
    `sender`."""
    domain = sender.rpartition("@")[2]
    with console.waiting("checking: Microsoft sign-in", once=True):
        found = account()
    signed_in = str((found.get("user") or {}).get("name") or "")
    organization = str(found.get("tenantDefaultDomain") or found.get("tenantDisplayName")
                       or signed_in.rpartition("@")[2])
    token = ""
    if signed_in:
        console.heading("Which Microsoft account should pdt use?")
        if choose([f"Use {signed_in} (organization {organization})",
                   "Sign in with another account"]) == 0:
            token = az_token()
    if token == "":
        console.say(f"pdt opens the Microsoft sign-in of {console.value(domain)} in your "
                    "browser. Sign in as an administrator.")
        with console.waiting("waiting: Microsoft sign-in in the browser"):
            done = az("login", "--tenant", domain, "--allow-no-subscriptions")
        if done.returncode != 0:
            raise SetupError((done.stdout + done.stderr).strip() or "the sign-in did not finish")
        token = az_token()
        if token == "":
            raise SetupError("the Microsoft sign-in gave no access to Microsoft Graph")
        organization = domain
    found = claims(token)
    user = str(found.get("upn") or found.get("unique_name") or "your account")
    console.say(f"Signed in to Microsoft as {console.value(user)}.")
    return (str(found.get("tid") or ""), user, organization,
            lambda method, path, body=None: graph_call(token, method, path, body))


def mailbox(call: Callable, address: str) -> bool:
    """True when `address` is the user principal name, the mail, or an smtp: proxy address of
    a user of the organization. A shared mailbox is a user too."""
    quoted = address.replace("'", "''")
    found = call("GET", f"/users?$filter=userPrincipalName eq '{quoted}' or mail eq '{quoted}' "
                        f"or proxyAddresses/any(a:a eq 'smtp:{quoted}')"
                        "&$select=userPrincipalName,mail,proxyAddresses")["value"]
    wanted = address.lower()
    return any(str(user.get("userPrincipalName") or "").lower() == wanted
               or str(user.get("mail") or "").lower() == wanted
               or f"smtp:{wanted}" in [str(item).lower() for item in user.get("proxyAddresses") or []]
               for user in found)


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
    """Write the key file readable by its owner alone."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as f:
        f.write(data)


def git(project: Path, *args: str) -> int | None:
    """The exit code of one git command in the project folder, or None with no git."""
    try:
        return subprocess.run(["git", *args], cwd=project, capture_output=True).returncode
    except OSError:
        return None


def keep_out_of_git(project: Path, path: Path) -> None:
    """Make sure git ignores the key file at `path`, and warn when git already tracks it.
    The folder of the file gets a .gitignore for everything in it when it has none. When
    git does not report the file as ignored (also with no repository or no git, so a later
    `git init` cannot stage the key), its name goes into that .gitignore."""
    path.parent.mkdir(parents=True, exist_ok=True)
    ignore = path.parent / ".gitignore"
    shown = path.relative_to(project).as_posix()
    if not ignore.exists():
        ignore.write_text("*\n")
    elif git(project, "check-ignore", "-q", "--no-index", shown) != 0:
        text = ignore.read_text()
        if f"/{path.name}" not in text.splitlines():
            ignore.write_text(text + ("" if text.endswith("\n") or text == "" else "\n")
                              + f"/{path.name}\n")
    if git(project, "ls-files", "--error-unmatch", shown) == 0:
        command = f"git rm --cached {shown}"
        console.warn(f"git tracks {console.value(shown)}, which holds a private key. Take it "
                     f"out of the repository with {console.value(command)}.")


def ensure(project: Path, tenant_id: str, user: str, call: Callable,
           organization: str = "") -> dict[str, str] | None:
    global granted_now
    client_id = alert_message.secret_value("PDT_EMAIL_CLIENT_ID")
    saved_tenant = alert_message.secret_value("PDT_EMAIL_TENANT_ID")
    if saved_tenant and tenant_id and saved_tenant.lower() != tenant_id.lower():
        if not console.confirm(f"The .env file holds the Microsoft 365 settings of organization "
                               f"{saved_tenant}. Replace them with organization "
                               f"{organization or tenant_id}?"):
            console.say("pdt changed nothing.")
            console.next_steps([("pdt alerts setup", "run the setup again")])
            return None
        client_id = ""
    needs = ("create an app registration", "Application Developer or Global Administrator")
    registration = None
    try:
        found = []
        if client_id:
            found = call("GET", f"/applications?$filter=appId eq '{client_id}'&$select=id")["value"]
        if not found:
            named = call("GET", f"/applications?$filter=displayName eq '{REGISTRATION}'"
                                "&$select=id,appId,tags")["value"]
            found = [app for app in named if TAG in (app.get("tags") or [])]
            if named and not found:
                console.failed(f"This Microsoft 365 organization already has an app registration "
                               f"named {console.value(REGISTRATION)} that pdt did not create "
                               f"(application ID {console.value(named[0]['appId'])}). pdt "
                               "changed nothing.")
                console.say("Rename or delete that registration in the Microsoft Entra admin "
                            "center, then run the setup again. Or, to let pdt use it, set this "
                            f"value in the {console.value('.env')} file of the project, then run "
                            "the setup again:")
                console.command(f"PDT_EMAIL_CLIENT_ID={named[0]['appId']}")
                return None
        if found:
            registration = call("GET", f"/applications/{found[0]['id']}?$select=id,appId,"
                                       "keyCredentials,requiredResourceAccess,web")
        else:
            registration = call("POST", "/applications", {
                "displayName": REGISTRATION, "signInAudience": "AzureADMyOrg",
                "tags": [TAG], "web": {"redirectUris": [REDIRECT_URI]}})
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
        keep_out_of_git(project, path)
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
        steps = [("pdt alerts setup", "run the setup again, choose Sign in with another "
                                      "account, and sign in with an account that has the role")]
        if registration is not None and needs[0].startswith("give"):
            link = (f"https://login.microsoftonline.com/{tenant_id}/adminconsent"
                    f"?client_id={registration['appId']}"
                    f"&redirect_uri={urllib.parse.quote(REDIRECT_URI, safe='')}")
            console.say("Or an administrator opens this link and approves the permission. The "
                        "browser then shows a page that does not load, which is correct:")
            console.command(link)
            steps = [("pdt alerts setup", "run the setup again after that")]
        console.next_steps(steps, "Then:")
        return None
    return {"PDT_EMAIL_TENANT_ID": tenant_id, "PDT_EMAIL_CLIENT_ID": app_id,
            "PDT_EMAIL_PRIVATE_KEY_PATH": CERTIFICATE}


def microsoft_365(project: Path, sender: str) -> tuple[str, dict[str, str]] | None:
    """The sender address and the env vars of the microsoft-365 method after the guided
    setup, or None when the setup did not finish. While the sender address is not a mailbox
    of the organization, it asks for another one, and an empty answer stops the setup."""
    try:
        tenant_id, user, organization, call = sign_in(sender)
        while not mailbox(call, sender):
            console.warn(f"{console.value(sender)} is not a mailbox in the Microsoft 365 "
                         f"organization {console.value(organization)}.")
            sender = console.ask("Which address should the email come from?")
            if sender == "":
                return None
        values = ensure(project, tenant_id, user, call, organization)
        return None if values is None else (sender, values)
    except (SetupError, GraphError) as e:
        console.failed(f"Microsoft 365 is not set up: {console.escape(str(e))}")
        return None


class ArmError(Exception):
    def __init__(self, status: int, text: str):
        super().__init__(text)
        self.status = status


def arm_call(token: str, method: str, path: str, body: dict | None = None) -> dict:
    request = urllib.request.Request(
        ARM + path, method=method, data=json.dumps(body).encode() if body is not None else None,
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
        raise ArmError(e.code, f"Azure answered HTTP {e.code}: {text[:300]}") from None
    except urllib.error.URLError as e:
        raise SetupError(f"Azure could not be reached: {e.reason}") from None
    return json.loads(text) if text else {}


def azure_sign_in() -> tuple[str, Callable]:
    """The subscription id that the user chooses from the Azure CLI sign-in in AZURE_DIR, and
    `call` for Azure Resource Manager."""
    with console.waiting("checking: Azure sign-in", once=True):
        found = account()
    signed_in = str((found.get("user") or {}).get("name") or "")
    use = False
    if signed_in:
        console.heading("Which Azure account should pdt use?")
        use = choose([f"Use {signed_in}", "Sign in with another account"]) == 0
    if not use:
        console.say("pdt opens the Azure sign-in in your browser.")
        with console.waiting("waiting: Azure sign-in in the browser"):
            done = az("login")
        if done.returncode != 0:
            raise SetupError((done.stdout + done.stderr).strip() or "the sign-in did not finish")
    done = az("account", "list", "--query", "[?state=='Enabled']", "--output", "json")
    try:
        subscriptions = json.loads(done.stdout) if done.returncode == 0 else []
    except ValueError:
        subscriptions = []
    # A sign-in with no subscription lists its tenant as an entry with the tenant's id.
    subscriptions = [item for item in subscriptions if item.get("id") != item.get("tenantId")]
    if not subscriptions:
        raise SetupError("the Azure account has no enabled subscription. Run pdt alerts setup "
                         "again and choose Sign in with another account")
    subscription = subscriptions[0]
    if len(subscriptions) > 1:
        console.heading("Which Azure subscription should hold the email service?")
        subscription = subscriptions[choose([f"{item['name']} ({item['id']})"
                                             for item in subscriptions])]
    done = az("account", "get-access-token", "--subscription", subscription["id"], "--query",
              "accessToken", "--output", "tsv")
    if done.returncode != 0 or done.stdout.strip() == "":
        raise SetupError("the Azure sign-in gave no access to Azure Resource Manager")
    token = done.stdout.strip().splitlines()[-1]
    user = str((subscription.get("user") or {}).get("name") or signed_in or "your account")
    console.say(f"Signed in to Azure as {console.value(user)}, subscription "
                f"{console.value(subscription['name'])}.")
    return subscription["id"], lambda method, path, body=None: arm_call(token, method, path, body)


def data_location(region: str) -> str:
    """The data location of Azure Communication Services nearest to an Azure region."""
    return next((place for part, place in DATA_LOCATIONS if part in region), "United States")


def tagged(resource: dict) -> bool:
    return (resource.get("tags") or {}).get("managed-by") == "pdt"


def provisioned(call: Callable, path: str, label: str) -> dict:
    """The resource at `path` once Azure finished creating or changing it."""
    deadline = time.monotonic() + PROVISION_WAIT_SECONDS
    with console.waiting(f"waiting: Azure to finish {label}"):
        while True:
            found = call("GET", path)
            state = (found.get("properties") or {}).get("provisioningState", "Succeeded")
            if state == "Succeeded":
                return found
            if state in ("Failed", "Canceled"):
                raise SetupError(f"Azure could not finish {label}: {state}")
            if time.monotonic() >= deadline:
                raise SetupError(f"Azure did not finish {label} in {PROVISION_WAIT_SECONDS} "
                                 "seconds. Run pdt alerts setup again")
            time.sleep(RETRY_SECONDS)


def ensure_resource(call: Callable, path: str, label: str, body: dict) -> dict:
    """The resource at `path`, created with `body` when it is missing."""
    try:
        found = call("GET", path)
        console.done(f"{label} is already there")
        return found
    except ArmError as e:
        if e.status != 404:
            raise
    call("PUT", path, body)
    found = provisioned(call, path, label)
    console.done(f"created {label}")
    return found


def full_name(name: str, domain: str) -> str:
    """The DNS name of a verification record, which Azure gives relative to the domain."""
    if name in ("", "@"):
        return domain
    if name == domain or name.endswith("." + domain):
        return name
    return f"{name}.{domain}"


def add_records(call: Callable, zone: dict, domain: str, records: dict) -> None:
    """Add each verification record to the Azure DNS zone, next to the records it holds."""
    for kind in VERIFY:
        record = records.get(kind)
        if not record:
            continue
        name = full_name(record["name"], domain)
        relative = "@" if name == zone["name"] else name[:-len(zone["name"]) - 1]
        path = f"{zone['id']}/{record['type']}/{relative}?api-version={DNS_API}"
        shown = f"the {record['type']} record {console.value(name)}"
        try:
            found = call("GET", path)["properties"]
        except ArmError as e:
            if e.status != 404:
                raise
            found = None
        if record["type"] == "CNAME":
            if found is None:
                call("PUT", path, {"properties": {"TTL": record.get("ttl") or 3600,
                                                  "CNAMERecord": {"cname": record["value"]}}})
                console.done(f"added {shown}")
            elif (found.get("CNAMERecord") or {}).get("cname", "").rstrip(".") == record["value"]:
                console.done(f"{shown} is already there")
            else:
                console.warn(f"{shown} points to another name. Change it to "
                             f"{console.value(record['value'])}.")
            continue
        values = ["".join(item.get("value") or []) for item in (found or {}).get("TXTRecords") or []]
        spf = [value for value in values if value.startswith("v=spf1")]
        if record["value"] in values:
            console.done(f"{shown} with {console.value(record['value'])} is already there")
        elif kind == "SPF" and spf:
            console.warn(f"{shown} holds the SPF record {console.value(spf[0])}. Add "
                         f"{console.value('include:spf.protection.outlook.com')} to it.")
        elif found is None:
            call("PUT", path, {"properties": {"TTL": record.get("ttl") or 3600,
                                              "TXTRecords": [{"value": [record["value"]]}]}})
            console.done(f"added {shown} with {console.value(record['value'])}")
        else:
            call("PATCH", path, {"properties": {"TXTRecords": [
                *found["TXTRecords"], {"value": [record["value"]]}]}})
            console.done(f"added {shown} with {console.value(record['value'])}")


def verify(call: Callable, subscription: str, domain: dict, created: bool) -> None:
    """Add the records of a domain that is not verified to its Azure DNS zone, or print them,
    and start the verification of each record that is not verified or in progress."""
    name = domain["name"]
    records = domain["properties"].get("verificationRecords") or {}
    states = domain["properties"].get("verificationStates") or {}
    zones = call("GET", f"/subscriptions/{subscription}/providers/Microsoft.Network/dnszones"
                        f"?api-version={DNS_API}")["value"]
    zones = [zone for zone in zones if name == zone["name"] or name.endswith("." + zone["name"])]
    zone = max(zones, key=lambda item: len(item["name"])) if zones else None
    if not created and not tagged(domain):
        console.failed(f"The email domain {console.value(name)} is not verified, and pdt did "
                       "not create it, so pdt changed nothing.")
        zone = None
    elif zone is not None:
        add_records(call, zone, name, records)
    if tagged(domain) and (zone is not None or not created):
        started = [kind for kind in VERIFY if kind in records and (states.get(kind) or {})
                   .get("status", "NotStarted") in ("NotStarted", "VerificationFailed")]
        for kind in started:
            call("POST", f"{domain['id']}/initiateVerification?api-version={ACS_API}",
                 {"verificationType": kind})
        if started:
            console.done(f"started the verification of {console.value(name)}")
        else:
            console.done(f"the verification of {console.value(name)} is in progress")
    if zone is None:
        console.say(f"Add these records to the DNS of {console.value(name)}:")
        for kind in VERIFY:
            if kind in records:
                console.say(f"  {console.value(full_name(records[kind]['name'], name))}  "
                            f"{console.value(records[kind]['type'])}  "
                            f"{console.value(records[kind]['value'])}")
    console.next_steps([("pdt alerts setup", "run the setup again after Azure verified the "
                                             "domain, which takes 15 to 30 minutes")])


def link(call: Callable, subscription: str, domain: dict, group: str, service_path: str,
         make_group: bool) -> str:
    """The id of a Communication Service that links `domain`. pdt uses one that links it
    already; else it links the domain to its own Communication Service, in `group`, which it
    creates first when `make_group` is true."""
    services = call("GET", f"/subscriptions/{subscription}/providers/Microsoft.Communication/"
                           f"communicationServices?api-version={ACS_API}")["value"]
    for service in services:
        linked = [item.lower() for item in service["properties"].get("linkedDomains") or []]
        if domain["id"].lower() in linked:
            console.done(f"the communication service {console.value(service['name'])} already "
                         f"sends from {console.value(domain['name'])}")
            return service["id"]
    if make_group:
        ensure_group(call, group)
    label = f"the communication service {console.value(service_path.rpartition('/')[2])}"
    path = f"{service_path}?api-version={ACS_API}"
    try:
        service = call("GET", path)
    except ArmError as e:
        if e.status != 404:
            raise
        call("PUT", path, {"location": "global", "tags": TAGS, "properties": {
            "dataLocation": domain["properties"]["dataLocation"], "linkedDomains": [domain["id"]]}})
        provisioned(call, path, label)
        console.done(f"created {label}")
        return service_path
    if not tagged(service):
        raise SetupError(f"{service['name']} in the resource group {RESOURCE_GROUP} has no "
                         f"tag {TAG}, so pdt does not change it")
    call("PATCH", path, {"properties": {"linkedDomains": [
        *(service["properties"].get("linkedDomains") or []), domain["id"]]}})
    provisioned(call, path, label)
    console.done(f"linked {console.value(domain['name'])} to {label}")
    return service_path


def ensure_group(call: Callable, group: str) -> None:
    ensure_resource(call, f"{group}?api-version={GROUP_API}",
                    f"the resource group {console.value(RESOURCE_GROUP)}",
                    {"location": regions.suggest_region("azure"), "tags": TAGS})


def ensure_email_service(call: Callable, group: str, email_path: str) -> None:
    ensure_group(call, group)
    ensure_resource(call, f"{email_path}?api-version={ACS_API}",
                    f"the email service {console.value(email_path.rpartition('/')[2])}",
                    {"location": "global", "tags": TAGS, "properties": {
                        "dataLocation": data_location(regions.suggest_region("azure"))}})


def acs_ensure(subscription: str, sender: str, call: Callable) -> tuple[str, str] | None:
    """The sender address and the connection string of a Communication Service that sends
    from the domain of `sender`, or from an Azure managed domain when `sender` is empty. It
    returns None when the domain is not verified yet."""
    group = f"/subscriptions/{subscription}/resourceGroups/{RESOURCE_GROUP}"
    name = f"pdt-email-{hashlib.sha256(subscription.encode()).hexdigest()[:8]}"
    email_path = f"{group}/providers/Microsoft.Communication/emailServices/{name}"
    service_path = f"{group}/providers/Microsoft.Communication/communicationServices/{name}"
    if sender == "":
        ensure_email_service(call, group, email_path)
        domain = ensure_resource(
            call, f"{email_path}/domains/{AZURE_DOMAIN}?api-version={ACS_API}",
            f"the Azure email domain {console.value(AZURE_DOMAIN)}",
            {"location": "global", "tags": TAGS, "properties": {"domainManagement": "AzureManaged"}})
        service_id = link(call, subscription, domain, group, service_path, False)
        sender = f"{SENDER_USERNAME}@{domain['properties']['fromSenderDomain']}"
        console.say(f"The Azure address {console.value(sender)} sends at most "
                    f"{email_alert.ACS_LIMITS['azure'][1]} messages each hour for the whole "
                    "subscription.")
    else:
        username, _, domain_name = sender.rpartition("@")
        domain = None
        for service in call("GET", f"/subscriptions/{subscription}/providers/"
                                   f"Microsoft.Communication/emailServices?api-version={ACS_API}"
                            )["value"]:
            for found in call("GET", f"{service['id']}/domains?api-version={ACS_API}")["value"]:
                if found["name"].lower() == domain_name.lower():
                    domain = found
        created = domain is None
        if created:
            ensure_email_service(call, group, email_path)
            domain = ensure_resource(
                call, f"{email_path}/domains/{domain_name}?api-version={ACS_API}",
                f"the email domain {console.value(domain_name)}",
                {"location": "global", "tags": TAGS,
                 "properties": {"domainManagement": "CustomerManaged"}})
        if not all((domain["properties"].get("verificationStates") or {}).get(kind, {})
                   .get("status") == "Verified" for kind in VERIFY):
            verify(call, subscription, domain, created)
            return None
        if username.lower() != SENDER_USERNAME.lower():
            path = f"{domain['id']}/senderUsernames/{username}?api-version={ACS_API}"
            shown = f"the sender user name {console.value(username)}"
            try:
                call("GET", path)
                console.done(f"{shown} is already there")
            except ArmError as e:
                if e.status != 404:
                    raise
                if not tagged(domain):
                    raise SetupError(f"{username} is not a sender user name of {domain_name}, "
                                     "and pdt did not create that domain, so pdt does not "
                                     "change it") from None
                call("PUT", path, {"properties": {"username": username}})
                console.done(f"added {shown} to {console.value(domain_name)}")
        service_id = link(call, subscription, domain, group, service_path, not created)
    keys = call("POST", f"{service_id}/listKeys?api-version={ACS_API}")
    return sender, keys["primaryConnectionString"]


def azure_communication_services(sender: str) -> tuple[str, dict[str, str]] | None:
    """The sender address and the env var of the azure-communication-services method after
    the guided setup, or None when the setup did not finish."""
    try:
        subscription, call = azure_sign_in()
        found = acs_ensure(subscription, sender, call)
    except (SetupError, ArmError) as e:
        console.failed(f"Azure Communication Services is not set up: {console.escape(str(e))}")
        return None
    if found is None:
        return None
    return found[0], {"PDT_ACS_CONNECTION_STRING": found[1]}


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
