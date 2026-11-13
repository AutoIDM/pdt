import base64
import json
import os
import re
import shutil
import subprocess

import pytest

from pdt import alerts_cli, config, console, deploy, email_alert, email_setup

TENANT = "11111111-1111-1111-1111-111111111111"
MAIL_SEND = "b633e1c5-f582-4048-a93e-9f11b44c7e96"
GRAPH_PRINCIPAL = "graph-principal"
ENV_NAMES = ("PDT_EMAIL_TENANT_ID", "PDT_EMAIL_CLIENT_ID", "PDT_EMAIL_PRIVATE_KEY_PATH",
             "PDT_EMAIL_PRIVATE_KEY_B64")


class Organization:
    """A stand-in for Microsoft Graph: the registrations, service principals, and granted
    permissions of one organization. `refuse` holds the start of each `METHOD path` that
    answers HTTP 403, and `writes` each change that it took."""

    def __init__(self):
        self.applications = {}
        self.principals = {}
        self.grants = []
        self.refuse = []
        self.writes = []
        self.users = [{"userPrincipalName": "pdt-alerts@example.com",
                       "mail": "pdt-alerts@example.com"}]

    def call(self, method, path, body=None):
        if any(f"{method} {path}".startswith(start) for start in self.refuse):
            raise email_setup.GraphError(403, "Microsoft Graph answered HTTP 403: "
                                              "Authorization_RequestDenied")
        if method != "GET":
            self.writes.append(f"{method} {path.split('/')[1]}")
        if method == "GET" and path.startswith("/users?$filter="):
            return {"value": self.users}
        if method == "GET" and path.startswith("/applications?$filter="):
            key, value = re.search(r"filter=(\w+) eq '([^']*)'", path).groups()
            return {"value": [{"id": app["id"], "appId": app["appId"], "tags": app.get("tags", [])}
                              for app in self.applications.values()
                              if app[key] == value]}
        if method == "GET" and path.startswith("/applications/"):
            return self.applications[path.split("/")[2].split("?")[0]]
        if method == "POST" and path == "/applications":
            app = {"id": "object-1", "appId": "client-1", "keyCredentials": [],
                   "requiredResourceAccess": [], **body}
            self.applications[app["id"]] = app
            return app
        if method == "PATCH" and path.startswith("/applications/"):
            self.applications[path.split("/")[2]].update(body)
            return {}
        if method == "GET" and path.startswith(f"/servicePrincipals(appId='{email_setup.GRAPH_APP_ID}')"):
            return {"id": GRAPH_PRINCIPAL, "appRoles": [
                {"id": "other", "value": "Mail.Read", "allowedMemberTypes": ["Application"]},
                {"id": MAIL_SEND, "value": "Mail.Send", "allowedMemberTypes": ["Application"]}]}
        if method == "GET" and path.startswith("/servicePrincipals(appId="):
            app_id = re.search(r"appId='([^']*)'", path).group(1)
            if app_id not in self.principals:
                raise email_setup.GraphError(404, "Microsoft Graph answered HTTP 404")
            return {"id": self.principals[app_id]}
        if method == "POST" and path == "/servicePrincipals":
            self.principals[body["appId"]] = "principal-1"
            return {"id": "principal-1"}
        if method == "GET" and path.endswith("/appRoleAssignments"):
            return {"value": [grant for grant in self.grants
                              if grant["principalId"] == path.split("/")[2]]}
        if method == "POST" and path.endswith("/appRoleAssignedTo"):
            self.grants.append(body)
            return body
        raise AssertionError(f"{method} {path}")


@pytest.fixture
def organization(project, monkeypatch):
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(email_setup, "granted_in_this_process", False)
    monkeypatch.setattr(email_setup.time, "sleep", lambda _seconds: None)
    found = Organization()
    monkeypatch.setattr(email_setup, "sign_in",
                        lambda _sender: (TENANT, "admin@example.com", "example.com", found.call))
    return found


def test_a_first_run_makes_the_registration_the_certificate_and_the_permission(
        project, organization, capsys):
    values = email_setup.ensure(project, TENANT, "admin@example.com", organization.call)
    assert values == {"PDT_EMAIL_TENANT_ID": TENANT, "PDT_EMAIL_CLIENT_ID": "client-1",
                      "PDT_EMAIL_PRIVATE_KEY_PATH": ".secrets/pdt-email.pfx"}
    assert capsys.readouterr().out.splitlines() == [
        "created the app registration pdt-email",
        "created the certificate .secrets/pdt-email.pfx",
        "gave pdt-email the permission Mail.Send"]
    assert organization.writes == [
        "POST applications", "POST servicePrincipals", "PATCH applications",
        "PATCH applications", "POST servicePrincipals"]
    app = organization.applications["object-1"]
    assert (app["displayName"], app["signInAudience"]) == ("pdt-email", "AzureADMyOrg")
    assert app["requiredResourceAccess"] == [{
        "resourceAppId": email_setup.GRAPH_APP_ID,
        "resourceAccess": [{"id": MAIL_SEND, "type": "Role"}]}]
    assert organization.grants == [{"principalId": "principal-1", "resourceId": GRAPH_PRINCIPAL,
                                    "appRoleId": MAIL_SEND}]
    key = project / ".secrets" / "pdt-email.pfx"
    assert email_setup.thumbprints(app["keyCredentials"]) == {
        email_setup.hashlib.sha1(email_setup.saved_certificate(key)).hexdigest().upper()}
    assert (project / ".secrets" / ".gitignore").read_text() == "*\n"
    if os.name != "nt":
        assert key.stat().st_mode & 0o777 == 0o600
    assert email_setup.granted_in_this_process


def test_a_new_certificate_is_valid_for_ten_years():
    from cryptography import x509
    _pfx, der = email_setup.new_certificate()
    certificate = x509.load_der_x509_certificate(der)
    assert (certificate.not_valid_after_utc - certificate.not_valid_before_utc).days == 3650


def test_a_second_run_changes_nothing_in_microsoft_365_or_in_the_project(
        project, organization, monkeypatch, capsys):
    values = email_setup.ensure(project, TENANT, "admin@example.com", organization.call)
    key = (project / ".secrets" / "pdt-email.pfx").read_bytes()
    organization.writes.clear()
    monkeypatch.setattr(email_setup, "granted_in_this_process", False)
    capsys.readouterr()
    assert email_setup.ensure(project, TENANT, "admin@example.com", organization.call) == values
    assert capsys.readouterr().out.splitlines() == [
        "the app registration pdt-email is already there",
        "the certificate .secrets/pdt-email.pfx is already there",
        "pdt-email already has the permission Mail.Send"]
    assert organization.writes == []
    assert (project / ".secrets" / "pdt-email.pfx").read_bytes() == key
    assert not email_setup.granted_in_this_process


def test_an_account_that_cannot_grant_gets_the_role_and_the_consent_link_and_a_later_run_goes_on(
        project, organization, capsys):
    organization.refuse = ["POST /servicePrincipals/graph-principal/appRoleAssignedTo"]
    assert email_setup.ensure(project, TENANT, "user@example.com", organization.call) is None
    out = capsys.readouterr().out
    assert ("user@example.com cannot give pdt-email the permission Mail.Send in this "
            "Microsoft 365 organization.") in out
    assert "Privileged Role Administrator or Global Administrator" in out
    assert (f"  https://login.microsoftonline.com/{TENANT}/adminconsent?client_id=client-1"
            "&redirect_uri=http%3A%2F%2Flocalhost\n") in out
    assert organization.grants == [] and (project / ".secrets" / "pdt-email.pfx").is_file()
    organization.refuse = []
    organization.writes.clear()
    assert email_setup.ensure(project, TENANT, "admin@example.com", organization.call) is not None
    assert organization.writes == ["POST servicePrincipals"]
    assert len(organization.applications) == 1


def unmarked(organization):
    """A registration named pdt-email that pdt did not create."""
    organization.applications["foreign"] = {
        "id": "foreign", "appId": "client-9", "displayName": "pdt-email", "keyCredentials": [],
        "requiredResourceAccess": [], "tags": []}
    organization.principals["client-9"] = "principal-9"


def test_a_registration_with_the_name_and_no_mark_stops_the_setup_and_changes_nothing(
        project, organization, capsys):
    unmarked(organization)
    assert email_setup.ensure(project, TENANT, "admin@example.com", organization.call) is None
    out = capsys.readouterr().out
    assert ("already has an app registration named pdt-email that pdt did not create "
            "(application ID client-9). pdt changed nothing.") in out
    assert "set this value in the .env file of the project" in " ".join(out.split())
    assert "  PDT_EMAIL_CLIENT_ID=client-9\n" in out
    assert organization.writes == [] and not (project / ".secrets").exists()


def test_a_registration_with_no_mark_is_used_when_the_saved_client_id_names_it(
        project, organization, monkeypatch):
    unmarked(organization)
    monkeypatch.setenv("PDT_EMAIL_CLIENT_ID", "client-9")
    values = email_setup.ensure(project, TENANT, "admin@example.com", organization.call)
    assert values["PDT_EMAIL_CLIENT_ID"] == "client-9"
    assert len(organization.applications) == 1


def test_a_second_project_finds_the_registration_that_pdt_marked(
        project, organization, tmp_path_factory):
    email_setup.ensure(project, TENANT, "admin@example.com", organization.call)
    unmarked(organization)
    assert organization.applications["object-1"]["tags"] == ["managed-by=pdt"]
    organization.writes.clear()
    second = tmp_path_factory.mktemp("second")
    values = email_setup.ensure(second, TENANT, "admin@example.com", organization.call)
    assert values["PDT_EMAIL_CLIENT_ID"] == "client-1"
    assert organization.writes == ["PATCH applications"]
    assert len(organization.applications["object-1"]["keyCredentials"]) == 2


OTHER_TENANT = "22222222-2222-2222-2222-222222222222"


def other_organization_saved(project):
    """A .env file and a certificate of a setup in another organization."""
    (project / ".env").write_text(f"PDT_EMAIL_TENANT_ID={OTHER_TENANT}\n"
                                  "PDT_EMAIL_CLIENT_ID=client-9\n"
                                  "PDT_EMAIL_PRIVATE_KEY_PATH=.secrets/pdt-email.pfx\n")
    pfx, _certificate = email_setup.new_certificate()
    email_setup.write_private(project / ".secrets" / "pdt-email.pfx", pfx)
    return (project / ".secrets" / "pdt-email.pfx").read_bytes()


def test_a_sign_in_to_another_organization_replaces_the_saved_one_after_yes(
        project, organization, monkeypatch, capsys):
    key = other_organization_saved(project)
    asked = []
    queue = answers(monkeypatch, "3 1", "", "", "it-ops@example.com", "2",
                    "pdt-alerts@example.com", "y", "n")
    monkeypatch.setattr(alerts_cli.console, "confirm",
                        lambda question, *_a, **_k: asked.append(question) or queue.pop(0) == "y")
    assert alerts_cli.setup() == 0
    assert queue == []
    assert asked[0] == (f"The .env file holds the Microsoft 365 settings of organization "
                        f"{OTHER_TENANT}. Replace them with organization example.com?")
    assert "added the certificate .secrets/pdt-email.pfx to pdt-email" in capsys.readouterr().out
    assert organization.writes[0] == "POST applications"
    assert (project / ".secrets" / "pdt-email.pfx").read_bytes() == key
    assert (project / ".env").read_text() == (
        f"PDT_EMAIL_TENANT_ID={TENANT}\nPDT_EMAIL_CLIENT_ID=client-1\n"
        "PDT_EMAIL_PRIVATE_KEY_PATH=.secrets/pdt-email.pfx\n")


def test_a_sign_in_to_another_organization_changes_nothing_after_no(
        project, organization, monkeypatch, capsys):
    key = other_organization_saved(project)
    env = (project / ".env").read_text()
    queue = answers(monkeypatch, "3 1", "", "", "it-ops@example.com", "2",
                    "pdt-alerts@example.com", "")
    assert alerts_cli.setup() == 1
    assert queue == []
    out = capsys.readouterr().out
    assert "pdt changed nothing." in out and "  pdt alerts setup" in out
    assert organization.writes == []
    assert (project / ".env").read_text() == env
    assert (project / ".secrets" / "pdt-email.pfx").read_bytes() == key
    assert "alerts" not in config.load_yaml(project / "pdt.yml")


def test_a_sign_in_to_another_organization_removes_the_saved_key_value(
        project, organization, monkeypatch, capsys):
    other_organization_saved(project)
    with (project / ".env").open("a") as env:
        env.write("PDT_EMAIL_PRIVATE_KEY_B64=b2xk\n")
    answers(monkeypatch, "3 1", "", "", "it-ops@example.com", "2", "pdt-alerts@example.com",
            "y", "n")
    assert alerts_cli.setup() == 0
    assert "removed PDT_EMAIL_PRIVATE_KEY_B64 from .env" in capsys.readouterr().out.splitlines()
    assert (project / ".env").read_text() == (
        f"PDT_EMAIL_TENANT_ID={TENANT}\nPDT_EMAIL_CLIENT_ID=client-1\n"
        "PDT_EMAIL_PRIVATE_KEY_PATH=.secrets/pdt-email.pfx\n")


def git(folder, *args):
    return subprocess.run(["git", *args], cwd=folder, capture_output=True, text=True)


needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


@needs_git
def test_a_secrets_folder_with_its_own_gitignore_gets_the_certificate_name(project, organization):
    git(project, "init", "-q")
    (project / ".secrets").mkdir()
    (project / ".secrets" / ".gitignore").write_text("*.key")
    email_setup.ensure(project, TENANT, "admin@example.com", organization.call)
    assert (project / ".secrets" / ".gitignore").read_text() == "*.key\n/pdt-email.pfx\n"
    assert git(project, "check-ignore", "-q", ".secrets/pdt-email.pfx").returncode == 0
    email_setup.ensure(project, TENANT, "admin@example.com", organization.call)
    assert (project / ".secrets" / ".gitignore").read_text() == "*.key\n/pdt-email.pfx\n"


def test_a_secrets_folder_with_its_own_gitignore_and_no_repository_gets_the_certificate_name(
        project, organization):
    (project / ".secrets").mkdir()
    (project / ".secrets" / ".gitignore").write_text("*.key")
    email_setup.ensure(project, TENANT, "admin@example.com", organization.call)
    assert (project / ".secrets" / ".gitignore").read_text() == "*.key\n/pdt-email.pfx\n"
    email_setup.ensure(project, TENANT, "admin@example.com", organization.call)
    assert (project / ".secrets" / ".gitignore").read_text() == "*.key\n/pdt-email.pfx\n"


@needs_git
def test_a_certificate_that_git_tracks_gets_a_warning(project, organization, capsys):
    git(project, "init", "-q")
    email_setup.ensure(project, TENANT, "admin@example.com", organization.call)
    assert "git tracks" not in capsys.readouterr().out
    git(project, "add", "-f", ".secrets/pdt-email.pfx")
    email_setup.ensure(project, TENANT, "admin@example.com", organization.call)
    assert ("git tracks .secrets/pdt-email.pfx, which holds a private key. Take it out "
            "of the repository with git rm --cached .secrets/pdt-email.pfx.") in " ".join(
                capsys.readouterr().out.split())


def test_an_account_that_cannot_register_an_app_gets_the_role_and_no_link(
        project, organization, capsys):
    organization.refuse = ["POST /applications"]
    assert email_setup.ensure(project, TENANT, "user@example.com", organization.call) is None
    out = capsys.readouterr().out
    assert "user@example.com cannot create an app registration" in out
    assert "adminconsent" not in out and not (project / ".secrets").exists()


def answers(monkeypatch, *replies):
    queue = list(replies)
    monkeypatch.setattr(alerts_cli.console, "ask", lambda *_a, **_k: queue.pop(0))
    monkeypatch.setattr(alerts_cli.console, "confirm", lambda *_a, **_k: queue.pop(0) == "y")
    monkeypatch.setattr(alerts_cli, "can_prompt", lambda _interactive: True)
    return queue


def test_setup_with_microsoft_365_asks_for_no_value_and_a_second_setup_writes_nothing(
        project, organization, monkeypatch):
    queue = answers(monkeypatch, "3 1", "", "", "it-ops@example.com", "2",
                    "pdt-alerts@example.com", "n")
    assert alerts_cli.setup() == 0
    assert queue == []
    assert config.load_yaml(project / "pdt.yml")["alerts"] == [{
        "type": "email", "method": "microsoft-365", "from": "pdt-alerts@example.com",
        "to": ["it-ops@example.com"],
        "events": ["app:deploy-completed", "app:deploy-failed", "job:run-failed"]}]
    assert (project / ".env").read_text() == (
        f"PDT_EMAIL_TENANT_ID={TENANT}\nPDT_EMAIL_CLIENT_ID=client-1\n"
        "PDT_EMAIL_PRIVATE_KEY_PATH=.secrets/pdt-email.pfx\n")
    files = {path: path.stat().st_mtime_ns for path in (
        project / "pdt.yml", project / ".env", project / ".secrets" / "pdt-email.pfx")}
    organization.writes.clear()
    queue = answers(monkeypatch, "", "", "y", "it-ops@example.com", "2",
                    "pdt-alerts@example.com", "n")
    assert alerts_cli.setup() == 0
    assert queue == [] and organization.writes == []
    assert {path: path.stat().st_mtime_ns for path in files} == files


def test_the_test_message_waits_and_tries_again_only_after_this_run_gave_the_permission(
        monkeypatch):
    entry = {"type": "email", "method": "microsoft-365", "from": "pdt-alerts@example.com",
             "to": ["it-ops@example.com"]}
    tries = []

    def send(_entry, _project):
        tries.append(1)
        if len(tries) < 3:
            raise RuntimeError("Microsoft Graph answered HTTP 403: ErrorAccessDenied")

    monkeypatch.setattr(email_setup.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(email_alert, "send_ready", send)
    monkeypatch.setattr(email_setup, "granted_in_this_process", False)
    with pytest.raises(RuntimeError, match="HTTP 403"):
        email_setup.send_test(entry, "contoso-jobs")
    assert len(tries) == 1
    monkeypatch.setattr(email_setup, "granted_in_this_process", True)
    email_setup.send_test(entry, "contoso-jobs")
    assert len(tries) == 3


def test_the_test_message_reports_the_real_error_when_the_wait_ends(monkeypatch):
    entry = {"type": "email", "method": "microsoft-365", "from": "pdt-alerts@example.com",
             "to": ["it-ops@example.com"]}

    def send(_entry, _project):
        raise RuntimeError("Microsoft Graph answered HTTP 403: ErrorAccessDenied")

    clock = iter(range(0, 10000, 50))
    monkeypatch.setattr(email_setup.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(email_setup.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(email_alert, "send_ready", send)
    monkeypatch.setattr(email_setup, "granted_in_this_process", True)
    with pytest.raises(RuntimeError, match="ErrorAccessDenied"):
        email_setup.send_test(entry, "contoso-jobs")


def fake_token(user, tenant):
    payload = base64.urlsafe_b64encode(json.dumps({"upn": user, "tid": tenant}).encode())
    return "header." + payload.decode().rstrip("=") + ".signature"


class AzureCli:
    """A stand-in for the Azure CLI that `email_setup.az` runs. It records each command with
    the AZURE_CONFIG_DIR it ran with, and holds the account that this folder signed in."""

    def __init__(self, account=None):
        self.account = account
        self.calls = []
        self.subscriptions = []

    def run(self, command, **kwargs):
        args = command[command.index("az") + 1:]
        self.calls.append((args, kwargs["env"].get("AZURE_CONFIG_DIR")))
        if args[0] == "login":
            domain = args[args.index("--tenant") + 1] if "--tenant" in args else "example.com"
            self.account = {"user": {"name": f"admin@{domain}"}, "tenantId": TENANT,
                            "tenantDefaultDomain": domain}
            return subprocess.CompletedProcess(command, 0, "[]", "")
        if self.account is None:
            return subprocess.CompletedProcess(command, 1, "", "Please run 'az login'.")
        if args[:2] == ["account", "show"]:
            return subprocess.CompletedProcess(command, 0, json.dumps(self.account), "")
        if args[:2] == ["account", "list"]:
            return subprocess.CompletedProcess(command, 0, json.dumps(self.subscriptions), "")
        return subprocess.CompletedProcess(
            command, 0, fake_token(self.account["user"]["name"], TENANT) + "\n", "")


@pytest.fixture
def azure_cli(project, monkeypatch):
    monkeypatch.delenv("AZURE_CONFIG_DIR", raising=False)
    found = AzureCli()
    monkeypatch.setattr(email_setup.subprocess, "run", found.run)
    monkeypatch.setattr(deploy, "announce_install", lambda _script: None)
    monkeypatch.setattr(email_setup.console, "can_menu", lambda: False)
    return found


def test_the_email_sign_in_uses_its_own_folder_and_the_organization_of_the_sender(
        azure_cli, capsys):
    tenant_id, user, organization, _call = email_setup.sign_in("pdt-alerts@contoso.example.com")
    assert (tenant_id, user, organization) == (TENANT, "admin@contoso.example.com",
                                               "contoso.example.com")
    assert [args[0] for args, _folder in azure_cli.calls] == ["account", "login", "account"]
    assert {folder for _args, folder in azure_cli.calls} == {str(email_setup.AZURE_DIR)}
    assert ["login", "--tenant", "contoso.example.com", "--allow-no-subscriptions"] in [
        args for args, _folder in azure_cli.calls]
    out = capsys.readouterr().out
    assert "Which Microsoft account" not in out
    assert ("pdt opens the Microsoft sign-in of contoso.example.com in your browser. Sign in as "
            "an administrator.") in " ".join(out.split())
    assert "AZURE_CONFIG_DIR" not in os.environ
    assert "AZURE_CONFIG_DIR" not in deploy.provider_env()


@pytest.mark.parametrize("answer, login", [("1", False), ("2", True)])
def test_a_saved_email_sign_in_gets_a_menu_to_use_it_or_to_sign_in_again(
        azure_cli, monkeypatch, capsys, answer, login):
    azure_cli.account = {"user": {"name": "old@example.com"}, "tenantId": TENANT,
                         "tenantDefaultDomain": "example.com"}
    monkeypatch.setattr(email_setup.console, "ask", lambda *_a, **_k: answer)
    _tenant, user, organization, _call = email_setup.sign_in("pdt-alerts@contoso.example.com")
    out = capsys.readouterr().out
    assert "  1) Use old@example.com (organization example.com)\n" in out
    assert "  2) Sign in with another account\n" in out
    logins = [args for args, _folder in azure_cli.calls if args[0] == "login"]
    if login:
        assert logins == [["login", "--tenant", "contoso.example.com", "--allow-no-subscriptions"]]
        assert (user, organization) == ("admin@contoso.example.com", "contoso.example.com")
    else:
        assert logins == []
        assert (user, organization) == ("old@example.com", "example.com")
    assert {folder for _args, folder in azure_cli.calls} == {str(email_setup.AZURE_DIR)}


def test_an_organization_with_no_domain_and_no_name_is_named_by_the_account_domain(
        azure_cli, monkeypatch, capsys):
    azure_cli.account = {"user": {"name": "old@contoso.example.com"}, "tenantId": TENANT}
    monkeypatch.setattr(email_setup.console, "ask", lambda *_a, **_k: "1")
    _tenant, _user, organization, _call = email_setup.sign_in("pdt-alerts@contoso.example.com")
    assert organization == "contoso.example.com"
    assert ("  1) Use old@contoso.example.com (organization contoso.example.com)\n"
            in capsys.readouterr().out)


def test_the_azure_sign_in_has_no_tenant_and_a_menu_of_the_enabled_subscriptions(
        azure_cli, monkeypatch, capsys):
    azure_cli.subscriptions = [
        {"id": "sub-1", "name": "Production", "tenantId": TENANT,
         "user": {"name": "admin@example.com"}},
        {"id": "sub-2", "name": "Sandbox", "tenantId": TENANT,
         "user": {"name": "admin@example.com"}},
        {"id": TENANT, "name": "N/A(tenant level account)", "tenantId": TENANT}]
    monkeypatch.setattr(email_setup.console, "ask", lambda *_a, **_k: "2")
    subscription, _call = email_setup.azure_sign_in()
    assert subscription == "sub-2"
    assert [args for args, _folder in azure_cli.calls if args[0] == "login"] == [["login"]]
    assert {folder for _args, folder in azure_cli.calls} == {str(email_setup.AZURE_DIR)}
    out = capsys.readouterr().out
    assert "  1) Production (sub-1)\n  2) Sandbox (sub-2)\n" in out
    assert "tenant level" not in out
    assert "Signed in to Azure as admin@example.com, subscription Sandbox." in out


@pytest.mark.parametrize("user", [
    {"userPrincipalName": "PDT-Alerts@example.com", "mail": "alerts@example.com"},
    {"userPrincipalName": "shared-1@example.com", "mail": "pdt-alerts@EXAMPLE.com"},
    {"userPrincipalName": "shared-1@example.com", "mail": "shared@example.com",
     "proxyAddresses": ["SMTP:shared@example.com", "smtp:Pdt-Alerts@example.com"]}])
def test_the_sender_is_a_mailbox_by_its_upn_its_mail_or_a_proxy_address(organization, user):
    organization.users = [user]
    assert email_setup.mailbox(organization.call, "pdt-alerts@example.com")


def test_a_user_with_only_an_x500_or_another_address_is_not_the_mailbox(organization):
    organization.users = [{"userPrincipalName": "other@example.com", "mail": "other@example.com",
                           "proxyAddresses": ["X500:pdt-alerts@example.com"]}]
    assert not email_setup.mailbox(organization.call, "pdt-alerts@example.com")


def test_a_user_with_no_exchange_mailbox_is_not_the_mailbox(organization):
    organization.users = [{"userPrincipalName": "pdt-alerts@example.com", "mail": None,
                           "proxyAddresses": []}]
    assert not email_setup.mailbox(organization.call, "pdt-alerts@example.com")


def test_a_sender_with_an_ampersand_gives_microsoft_graph_the_whole_filter(monkeypatch):
    import io
    import urllib.parse
    urls = []

    def urlopen(request, timeout=None):
        urls.append(request.full_url)
        return io.BytesIO(json.dumps({"value": [
            {"userPrincipalName": "r&d@example.com", "mail": "r&d@example.com"}]}).encode())

    monkeypatch.setattr(email_setup.urllib.request, "urlopen", urlopen)
    assert email_setup.mailbox(
        lambda method, path, body=None: email_setup.graph_call("token", method, path, body),
        "r&d@example.com")
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(urls[0]).query)
    assert query == {
        "$filter": ["userPrincipalName eq 'r&d@example.com' or mail eq 'r&d@example.com' "
                    "or proxyAddresses/any(a:a eq 'smtp:r&d@example.com')"],
        "$select": ["userPrincipalName,mail,proxyAddresses"]}
    assert urls[0].startswith("https://graph.microsoft.com/v1.0/users?$filter=userPrincipalName"
                              "%20eq%20'r%26d@example.com'")


def test_a_sender_that_is_no_mailbox_saves_nothing_and_is_asked_again(
        project, organization, monkeypatch, capsys):
    warned = []
    monkeypatch.setattr(email_setup.console, "warn", warned.append)
    queue = answers(monkeypatch, "3 1", "", "", "it-ops@example.com", "2",
                    "nobody@example.com", "")
    assert alerts_cli.setup() == 1
    assert queue == []
    assert warned == [f"{console.value('nobody@example.com')} is not a mailbox in the Microsoft "
                      f"365 organization {console.value('example.com')}."]
    assert organization.writes == [] and not (project / ".secrets").exists()
    assert not (project / ".env").exists()
    assert "alerts" not in config.load_yaml(project / "pdt.yml")


def test_a_sender_typed_again_that_is_a_mailbox_is_the_one_saved(
        project, organization, monkeypatch, capsys):
    asked = []
    queue = answers(monkeypatch, "3 1", "", "", "it-ops@example.com", "2",
                    "nobody@example.com", "pdt-alerts@example.com", "n")
    monkeypatch.setattr(alerts_cli.console, "confirm",
                        lambda question, *_a, **_k: asked.append(question) or queue.pop(0) == "y")
    assert alerts_cli.setup() == 0
    assert queue == []
    assert config.load_yaml(project / "pdt.yml")["alerts"][0]["from"] == "pdt-alerts@example.com"
    assert asked == ["Send a test message to email address it-ops@example.com now?"]
    assert ("saved  the email address it-ops@example.com entry under alerts: in pdt.yml"
            in capsys.readouterr().out.splitlines())


SUBSCRIPTION = "22222222-2222-2222-2222-222222222222"
NAME = "pdt-email-" + email_setup.hashlib.sha256(SUBSCRIPTION.encode()).hexdigest()[:8]
GROUP = f"/subscriptions/{SUBSCRIPTION}/resourceGroups/pdt-email"
PROVIDER = f"{GROUP}/providers/Microsoft.Communication"
OTHER = f"/subscriptions/{SUBSCRIPTION}/resourceGroups/mail/providers/Microsoft.Communication"
CONNECTION = "endpoint=https://acs.example.com/;accesskey=a2V5"
RECORDS = {
    "Domain": {"type": "TXT", "name": "example.com", "value": "ms-domain-verification=1",
               "ttl": 3600},
    "SPF": {"type": "TXT", "name": "example.com",
            "value": "v=spf1 include:spf.protection.outlook.com -all", "ttl": 3600},
    "DKIM": {"type": "CNAME", "name": "selector1-azurecomm-prod-net._domainkey",
             "value": "selector1-azurecomm-prod-net._domainkey.azurecomm.net", "ttl": 3600},
    "DKIM2": {"type": "CNAME", "name": "selector2-azurecomm-prod-net._domainkey",
              "value": "selector2-azurecomm-prod-net._domainkey.azurecomm.net", "ttl": 3600}}
VERIFIED = {kind: {"status": "Verified"} for kind in email_setup.VERIFY}


class Arm:
    """A stand-in for Azure Resource Manager: each resource by its id. `writes` holds each
    change as `METHOD <the last type in the path, or the action of a POST>`. `registration`
    holds each state that the provider Microsoft.Communication answers, in turn; the last
    one stays."""

    def __init__(self):
        self.resources = {}
        self.writes = []
        self.registration = ["Registered"]

    def add(self, path, body):
        name = path.rstrip("/").rpartition("/")[2]
        self.resources[path.lower()] = {"id": path, "name": name, **body}

    def call(self, method, path, body=None):
        path = path.partition("?")[0]
        key = path.lower()
        if method != "GET":
            parts = [part for part in path.split("/") if part]
            self.writes.append(f"{method} {parts[-1] if method == 'POST' else parts[-2]}")
        if key.endswith("/providers/microsoft.communication"):
            state = self.registration[0]
            if len(self.registration) > 1:
                self.registration.pop(0)
            return {"namespace": "Microsoft.Communication", "registrationState": state}
        if method == "POST" and key.endswith("/providers/microsoft.communication/register"):
            self.registration = ["Registering", "Registering", "Registered"]
            return {}
        if method == "POST" and key.endswith("/listkeys"):
            return {"primaryConnectionString": CONNECTION}
        if method == "POST" and key.endswith("/initiateverification"):
            return {}
        if method == "PUT":
            properties = dict(body.get("properties") or {})
            if properties.get("domainManagement") == "AzureManaged":
                properties.update(fromSenderDomain="0f1e2d3c.azurecomm.net",
                                  dataLocation="Europe", verificationStates=VERIFIED)
            elif properties.get("domainManagement") == "CustomerManaged":
                properties.update(fromSenderDomain=path.rpartition("/")[2], dataLocation="Europe",
                                  verificationRecords=RECORDS, verificationStates={})
            self.add(path, {**body, "properties": properties})
            return self.resources[key]
        if method == "PATCH":
            self.resources[key]["properties"].update(body["properties"])
            return self.resources[key]
        if key in self.resources:
            return self.resources[key]
        for kind in ("emailservices", "communicationservices", "dnszones"):
            if key.endswith(f"/providers/microsoft.communication/{kind}") or key.endswith(
                    f"/providers/microsoft.network/{kind}"):
                return {"value": [item for found, item in self.resources.items()
                                  if re.search(f"/{kind}/[^/]+$", found)]}
        if key.endswith("/domains"):
            return {"value": [item for found, item in self.resources.items()
                              if re.fullmatch(re.escape(key) + "/[^/]+", found)]}
        raise email_setup.ArmError(404, "Azure answered HTTP 404: ResourceNotFound")


@pytest.fixture
def arm(project, monkeypatch):
    monkeypatch.delenv("PDT_ACS_CONNECTION_STRING", raising=False)
    monkeypatch.setattr(email_setup.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(email_setup.regions, "suggest_region", lambda _provider: "westeurope")
    found = Arm()
    monkeypatch.setattr(email_setup, "azure_sign_in", lambda: (SUBSCRIPTION, found.call))
    return found


def acs_setup(monkeypatch, sender):
    return answers(monkeypatch, "3 1", "", "", "it-ops@example.com", "1", sender, "n")


def test_azure_communication_services_is_the_first_email_method():
    method = next(iter(alerts_cli.EMAIL_METHODS))
    assert (method, alerts_cli.EMAIL_METHODS[method][0]) == (
        "azure-communication-services",
        "Azure Communication Services: no mailbox needed; pdt creates the Azure email service")


def test_an_empty_sender_creates_the_azure_address_once_and_a_second_setup_changes_nothing(
        project, arm, monkeypatch, capsys):
    queue = acs_setup(monkeypatch, "")
    assert alerts_cli.setup() == 0
    assert queue == []
    assert arm.writes == ["PUT resourceGroups", "PUT emailServices", "PUT domains",
                          "PUT communicationServices", "POST listKeys"]
    assert arm.resources[f"{GROUP}".lower()]["location"] == "westeurope"
    assert arm.resources[f"{PROVIDER}/emailServices/{NAME}".lower()]["properties"] == {
        "dataLocation": "Europe"}
    service = arm.resources[f"{PROVIDER}/communicationServices/{NAME}".lower()]
    assert service["properties"]["linkedDomains"] == [
        f"{PROVIDER}/emailServices/{NAME}/domains/AzureManagedDomain"]
    assert all(item["tags"] == {"managed-by": "pdt"} for item in arm.resources.values())
    assert config.load_yaml(project / "pdt.yml")["alerts"] == [{
        "type": "email", "method": "azure-communication-services",
        "from": "DoNotReply@0f1e2d3c.azurecomm.net", "to": ["it-ops@example.com"],
        "events": ["app:deploy-completed", "app:deploy-failed", "job:run-failed"]}]
    assert (project / ".env").read_text() == f"PDT_ACS_CONNECTION_STRING={CONNECTION}\n"
    out = [" ".join(line.split()) for line in capsys.readouterr().out.splitlines()]
    start = out.index("created the resource group pdt-email")
    assert out[start:start + 5] == [
        "created the resource group pdt-email", f"created the email service {NAME}",
        "created the Azure email domain AzureManagedDomain",
        f"created the communication service {NAME}",
        "The Azure address DoNotReply@0f1e2d3c.azurecomm.net sends at most 10 messages each "
        "hour for the whole subscription."]
    arm.writes.clear()
    monkeypatch.setenv("PDT_ACS_CONNECTION_STRING", CONNECTION)
    queue = answers(monkeypatch, "", "", "y", "it-ops@example.com", "1", "", "n")
    assert alerts_cli.setup() == 0
    assert queue == [] and arm.writes == ["POST listKeys"]
    assert ("the resource group pdt-email is already there the email service "
            f"{NAME} is already there the Azure email domain AzureManagedDomain is already "
            f"there the communication service {NAME} already sends from AzureManagedDomain"
            ) in " ".join(capsys.readouterr().out.split())


def verified_domain(arm, path):
    arm.add(path, {"properties": {"dataLocation": "Europe", "verificationStates": VERIFIED,
                                  "verificationRecords": RECORDS}})


def test_a_service_that_links_the_verified_domain_of_the_sender_is_used_and_nothing_is_made(
        project, arm, monkeypatch, capsys):
    arm.add(f"{OTHER}/emailServices/mail", {"properties": {}})
    verified_domain(arm, f"{OTHER}/emailServices/mail/domains/example.com")
    arm.add(f"{OTHER}/communicationServices/mail", {"properties": {
        "linkedDomains": [f"{OTHER}/emailServices/mail/domains/example.com"]}})
    acs_setup(monkeypatch, "DoNotReply@example.com")
    assert alerts_cli.setup() == 0
    assert arm.writes == ["POST listKeys"]
    assert config.load_yaml(project / "pdt.yml")["alerts"][0]["from"] == "DoNotReply@example.com"
    assert "the communication service mail already sends from example.com" in capsys.readouterr().out


def test_a_verified_domain_with_no_service_is_linked_to_a_new_service_of_pdt(
        project, arm, monkeypatch, capsys):
    arm.add(f"{OTHER}/emailServices/mail", {"properties": {}})
    verified_domain(arm, f"{OTHER}/emailServices/mail/domains/example.com")
    acs_setup(monkeypatch, "DoNotReply@example.com")
    assert alerts_cli.setup() == 0
    assert arm.writes == ["PUT resourceGroups", "PUT communicationServices",
                          "POST listKeys"]
    service = arm.resources[f"{PROVIDER}/communicationServices/{NAME}".lower()]
    assert service["properties"] == {"dataLocation": "Europe", "linkedDomains": [
        f"{OTHER}/emailServices/mail/domains/example.com"]}
    assert (project / ".env").read_text() == f"PDT_ACS_CONNECTION_STRING={CONNECTION}\n"


def test_a_new_custom_domain_with_no_dns_zone_prints_the_records_and_stops(
        project, arm, monkeypatch, capsys):
    queue = answers(monkeypatch, "3 1", "", "", "it-ops@example.com", "1",
                    "DoNotReply@example.com")
    assert alerts_cli.setup() == 1
    assert queue == []
    assert arm.writes == ["PUT resourceGroups", "PUT emailServices", "PUT domains"]
    out = [" ".join(line.split()) for line in capsys.readouterr().out.splitlines()]
    assert "created the email domain example.com" in out
    assert out[out.index("Add these records to the DNS of example.com:") + 1:][:4] == [
        "example.com TXT ms-domain-verification=1",
        "example.com TXT v=spf1 include:spf.protection.outlook.com -all",
        "selector1-azurecomm-prod-net._domainkey.example.com CNAME "
        "selector1-azurecomm-prod-net._domainkey.azurecomm.net",
        "selector2-azurecomm-prod-net._domainkey.example.com CNAME "
        "selector2-azurecomm-prod-net._domainkey.azurecomm.net"]
    assert "pdt alerts setup" in " ".join(out)
    assert not (project / ".env").exists()
    assert "alerts" not in config.load_yaml(project / "pdt.yml")
    arm.writes.clear()
    answers(monkeypatch, "3 1", "", "", "it-ops@example.com", "1", "DoNotReply@example.com")
    assert alerts_cli.setup() == 1
    assert arm.writes == ["POST initiateVerification"] * 4
    assert "started the verification of example.com" in capsys.readouterr().out


def test_a_new_custom_domain_in_an_azure_dns_zone_gets_the_records_and_the_verification(
        project, arm, monkeypatch, capsys):
    zone = (f"/subscriptions/{SUBSCRIPTION}/resourceGroups/dns/providers/Microsoft.Network/"
            "dnszones/example.com")
    arm.add(zone, {"properties": {}})
    arm.add(f"{zone}/TXT/@", {"properties": {"TXTRecords": [{"value": ["google-site=1"]}]}})
    answers(monkeypatch, "3 1", "", "", "it-ops@example.com", "1", "DoNotReply@example.com")
    assert alerts_cli.setup() == 1
    assert arm.writes == ["PUT resourceGroups", "PUT emailServices", "PUT domains",
                          "PATCH TXT", "PATCH TXT", "PUT CNAME", "PUT CNAME"] + ["POST initiateVerification"] * 4
    assert arm.resources[f"{zone}/TXT/@".lower()]["properties"]["TXTRecords"] == [
        {"value": ["google-site=1"]}, {"value": ["ms-domain-verification=1"]},
        {"value": ["v=spf1 include:spf.protection.outlook.com -all"]}]
    assert arm.resources[f"{zone}/CNAME/selector1-azurecomm-prod-net._domainkey".lower()][
        "properties"]["CNAMERecord"] == {
            "cname": "selector1-azurecomm-prod-net._domainkey.azurecomm.net"}
    out = " ".join(capsys.readouterr().out.split())
    assert "started the verification of example.com" in out
    assert "Add these records" not in out
    assert not (project / ".env").exists()


def test_a_subscription_with_no_communication_provider_registers_it_before_the_first_write(
        project, arm, monkeypatch, capsys):
    arm.registration = ["NotRegistered"]
    acs_setup(monkeypatch, "")
    assert alerts_cli.setup() == 0
    assert arm.writes[:2] == ["POST register", "PUT resourceGroups"]
    out = [" ".join(line.split()) for line in capsys.readouterr().out.splitlines()]
    assert "registered the Azure resource provider Microsoft.Communication" in out
    arm.writes.clear()
    assert email_setup.acs_ensure(SUBSCRIPTION, "", arm.call) is not None
    assert "POST register" not in arm.writes


def test_a_domain_of_another_data_location_gets_its_own_service_of_pdt(
        project, arm, monkeypatch, capsys):
    arm.add(f"{PROVIDER}/communicationServices/{NAME}", {"tags": {"managed-by": "pdt"},
                                                         "properties": {
        "dataLocation": "United States", "linkedDomains": []}})
    arm.add(f"{OTHER}/emailServices/mail", {"properties": {}})
    verified_domain(arm, f"{OTHER}/emailServices/mail/domains/example.com")
    acs_setup(monkeypatch, "DoNotReply@example.com")
    assert alerts_cli.setup() == 0
    assert arm.writes == ["PUT resourceGroups", "PUT communicationServices", "POST listKeys"]
    assert arm.resources[f"{PROVIDER}/communicationServices/{NAME}".lower()]["properties"] == {
        "dataLocation": "United States", "linkedDomains": []}
    assert arm.resources[f"{PROVIDER}/communicationServices/{NAME}-europe".lower()][
        "properties"] == {"dataLocation": "Europe", "linkedDomains": [
            f"{OTHER}/emailServices/mail/domains/example.com"]}
    assert (f"created the communication service {NAME}-europe"
            in [" ".join(line.split()) for line in capsys.readouterr().out.splitlines()])


ACS_ENTRY = {"type": "email", "method": "azure-communication-services",
             "from": "DoNotReply@example.com", "to": ["it-ops@example.com"]}


def test_the_acs_test_message_waits_for_the_send_result_and_reports_a_failure(monkeypatch):
    results = [({"status": "Running"}, 2), ({"status": "Failed", "error": {
        "code": "EmailDroppedAllRecipientsSuppressed", "message": "Message dropped."}}, 0)]
    asked, slept = [], []
    monkeypatch.setattr(email_alert, "send_ready",
                        lambda _entry, _project: '{"id": "op-1", "status": "Running"}')
    monkeypatch.setattr(email_alert, "acs_status",
                        lambda operation: asked.append(operation) or results.pop(0))
    monkeypatch.setattr(email_setup.time, "sleep", slept.append)
    with pytest.raises(RuntimeError) as raised:
        email_setup.send_test(ACS_ENTRY, "contoso-jobs")
    assert str(raised.value) == ("Azure Communication Services did not send the email: "
                                 "EmailDroppedAllRecipientsSuppressed: Message dropped.")
    assert (asked, slept) == (["op-1", "op-1"], [2])


def test_the_acs_test_message_stops_waiting_after_60_seconds(monkeypatch):
    clock = iter(range(0, 1000, 25))
    monkeypatch.setattr(email_alert, "send_ready", lambda _entry, _project: '{"id": "op-1"}')
    monkeypatch.setattr(email_alert, "acs_status", lambda _operation: ({"status": "Running"}, 20))
    monkeypatch.setattr(email_setup.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(email_setup.time, "monotonic", lambda: next(clock))
    email_setup.send_test(ACS_ENTRY, "contoso-jobs")


def test_the_acs_status_is_a_signed_get_that_reads_retry_after(monkeypatch):
    import io
    requests = []

    class Answer(io.BytesIO):
        headers = {"Retry-After": "3"}

    def urlopen(request, timeout=None):
        requests.append(request)
        return Answer(b'{"id": "op-1", "status": "Running"}')

    monkeypatch.setenv("PDT_ACS_CONNECTION_STRING", CONNECTION)
    monkeypatch.setattr(email_alert.urllib.request, "urlopen", urlopen)
    assert email_alert.acs_status("op-1") == ({"id": "op-1", "status": "Running"}, 3)
    request, = requests
    assert (request.get_method(), request.full_url) == (
        "GET", "https://acs.example.com/emails/operations/op-1?api-version=2023-03-31")
    assert dict(request.header_items()) == {
        key.capitalize(): value for key, value in email_alert.acs_headers(
            request.full_url, b"", "a2V5", request.get_header("X-ms-date"), "GET").items()}
