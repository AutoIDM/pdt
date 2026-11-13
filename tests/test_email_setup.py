import os
import re

import pytest

from pdt import alerts_cli, config, email_alert, email_setup

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

    def call(self, method, path, body=None):
        if any(f"{method} {path}".startswith(start) for start in self.refuse):
            raise email_setup.GraphError(403, "Microsoft Graph answered HTTP 403: "
                                              "Authorization_RequestDenied")
        if method != "GET":
            self.writes.append(f"{method} {path.split('/')[1]}")
        if method == "GET" and path.startswith("/applications?$filter="):
            key, value = re.search(r"filter=(\w+) eq '([^']*)'", path).groups()
            return {"value": [{"id": app["id"]} for app in self.applications.values()
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
    monkeypatch.setattr(email_setup, "granted_now", False)
    monkeypatch.setattr(email_setup.time, "sleep", lambda _seconds: None)
    found = Organization()
    monkeypatch.setattr(email_setup, "sign_in",
                        lambda: (TENANT, "admin@example.com", found.call))
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
    assert email_setup.granted_now


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
    monkeypatch.setattr(email_setup, "granted_now", False)
    capsys.readouterr()
    assert email_setup.ensure(project, TENANT, "admin@example.com", organization.call) == values
    assert capsys.readouterr().out.splitlines() == [
        "the app registration pdt-email is already there",
        "the certificate .secrets/pdt-email.pfx is already there",
        "pdt-email already has the permission Mail.Send"]
    assert organization.writes == []
    assert (project / ".secrets" / "pdt-email.pfx").read_bytes() == key
    assert not email_setup.granted_now


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
    queue = answers(monkeypatch, "3 1", "", "", "it-ops@example.com", "1",
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
    queue = answers(monkeypatch, "", "", "y", "it-ops@example.com", "1",
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
    monkeypatch.setattr(email_setup, "granted_now", False)
    with pytest.raises(RuntimeError, match="HTTP 403"):
        email_setup.send_test(entry, "contoso-jobs")
    assert len(tries) == 1
    monkeypatch.setattr(email_setup, "granted_now", True)
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
    monkeypatch.setattr(email_setup, "granted_now", True)
    with pytest.raises(RuntimeError, match="ErrorAccessDenied"):
        email_setup.send_test(entry, "contoso-jobs")
