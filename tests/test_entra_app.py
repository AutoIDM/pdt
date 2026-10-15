"""`pdt entra <app>`: src/pdt/entra_app.py against a fake Microsoft Graph."""

import re
from pathlib import Path

import pytest
from dotenv import dotenv_values

from conftest import add_app
from pdt import cli, config, entra_app

GRAPH_SP = "graph-sp"
ROLES = {"User.Read.All": "role-user", "Group.Read.All": "role-group",
         "AuditLog.Read.All": "role-audit"}
ENV = ("PDT_AZURE_TENANT_ID", "PDT_AZURE_CLIENT_ID", "PDT_AZURE_CLIENT_SECRET",
       "PDT_AZURE_PRIVATE_KEY_B64", "PDT_AZURE_PRIVATE_KEY_PATH")


class FakeGraph:
    """The Graph calls entra_app makes, over an in-memory tenant."""

    def __init__(self, apps=(), sps=(), assignments=()):
        self.apps = [dict(app) for app in apps]
        self.sps = [dict(sp) for sp in sps]
        self.assignments = [dict(a) for a in assignments]
        self.calls = []

    def call(self, method, path, body=None):
        self.calls.append((method, path.split("?")[0], body))
        match = re.search(r"\$filter=([^&]+)", path)
        filter_ = match[1] if match else ""
        if method == "GET" and path.startswith("/servicePrincipals?"):
            if entra_app.GRAPH_APP_ID in filter_:
                return {"value": [{"id": GRAPH_SP, "appRoles": [
                    *({"id": i, "value": v, "allowedMemberTypes": ["Application"]} for v, i in ROLES.items()),
                    {"id": "role-delegated", "value": "Mail.Send", "allowedMemberTypes": ["User"]}]}]}
            return {"value": [sp for sp in self.sps if sp["appId"] in filter_]}
        if method == "GET" and path.startswith("/applications?"):
            key = "appId" if filter_.startswith("appId") else "displayName"
            return {"value": [a for a in self.apps if f"'{a[key]}'" in filter_]}
        if method == "GET" and path.endswith("/appRoleAssignments"):
            sp = path.split("/")[2]
            return {"value": [a for a in self.assignments if a["principalId"] == sp]}
        if method == "POST" and path == "/applications":
            app = {**body, "id": f"obj-{len(self.apps)}", "appId": f"client-{len(self.apps)}"}
            self.apps.append(app)
            return app
        if method == "PATCH" and path.startswith("/applications/"):
            next(a for a in self.apps if a["id"] == path.split("/")[2]).update(body)
            return {}
        if method == "POST" and path == "/servicePrincipals":
            sp = {"id": f"sp-{body['appId']}", "appId": body["appId"]}
            self.sps.append(sp)
            return sp
        if method == "POST" and path.endswith("/appRoleAssignments"):
            self.assignments.append(body)
            return body
        if method == "POST" and path.endswith("/addPassword"):
            return {"secretText": "new-secret"}
        raise AssertionError(f"unexpected Graph call {method} {path}")

    get = lambda self, path: self.call("GET", path)  # noqa: E731
    post = lambda self, path, body: self.call("POST", path, body)  # noqa: E731
    patch = lambda self, path, body: self.call("PATCH", path, body)  # noqa: E731

    def granted(self, sp_id):
        return sorted(a["appRoleId"] for a in self.assignments if a["principalId"] == sp_id)


def clear_azure_env(monkeypatch):
    # setenv first, so pytest restores each name after the test even when pdt sets it.
    for name in ENV:
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name)


@pytest.fixture
def app(project, monkeypatch):
    clear_azure_env(monkeypatch)
    add_app(project, "report", "schedule: daily\nentra:\n  graph_permissions: [User.Read.All, Group.Read.All]\n")
    return config.merged_app("report")


@pytest.fixture
def tenant(monkeypatch):
    """Sign-in, Graph, and the final token check, with every wait cut out."""
    state = {"graph": FakeGraph(), "verified": []}
    monkeypatch.setattr(entra_app, "sign_in", lambda *a: ("tenant-1", "admin@contoso.com", "token"))
    monkeypatch.setattr(entra_app, "Graph", lambda token: state["graph"])
    monkeypatch.setattr(entra_app, "verify", lambda *a: state["verified"].append(a) or [])
    monkeypatch.setattr(entra_app.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(entra_app.signal, "signal", lambda signum, handler: state.update(stop=handler))
    return state


def test_a_new_app_is_created_granted_and_saved_in_the_apps_env_file(app, tenant, capsys):
    assert entra_app.setup(app, assume_yes=True, device_code=False) == 0

    graph = tenant["graph"]
    (created,) = graph.apps
    assert created["displayName"] == "pdt-report"
    assert created["requiredResourceAccess"] == [{
        "resourceAppId": entra_app.GRAPH_APP_ID,
        "resourceAccess": [{"id": "role-user", "type": "Role"}, {"id": "role-group", "type": "Role"}]}]
    assert graph.granted("sp-client-0") == ["role-group", "role-user"]
    assert dotenv_values(app["dir"] / ".env") == {
        "PDT_AZURE_TENANT_ID": "tenant-1", "PDT_AZURE_CLIENT_ID": "client-0",
        "PDT_AZURE_CLIENT_SECRET": "new-secret"}
    assert oct((app["dir"] / ".env").stat().st_mode & 0o777) == "0o600"
    assert tenant["verified"] == [("tenant-1", "client-0", "new-secret", ["User.Read.All", "Group.Read.All"])]
    assert "new-secret" not in capsys.readouterr().out


def test_an_existing_app_gets_only_what_it_lacks_and_keeps_its_secret(app, tenant, monkeypatch):
    monkeypatch.setenv("PDT_AZURE_TENANT_ID", "tenant-1")
    monkeypatch.setenv("PDT_AZURE_CLIENT_ID", "client-9")
    monkeypatch.setenv("PDT_AZURE_CLIENT_SECRET", "old-secret")
    other = {"resourceAppId": "other-api", "resourceAccess": [{"id": "x", "type": "Scope"}]}
    tenant["graph"] = graph = FakeGraph(
        apps=[{"id": "obj-9", "appId": "client-9", "displayName": "AutoIDM", "requiredResourceAccess": [
            other, {"resourceAppId": entra_app.GRAPH_APP_ID, "resourceAccess": [
                {"id": "role-user", "type": "Role"}, {"id": "role-audit", "type": "Role"}]}]}],
        sps=[{"id": "sp-9", "appId": "client-9"}],
        assignments=[{"principalId": "sp-9", "resourceId": GRAPH_SP, "appRoleId": "role-user"}])

    assert entra_app.setup(app, assume_yes=True, device_code=False) == 0

    assert graph.apps[0]["requiredResourceAccess"] == [other, {
        "resourceAppId": entra_app.GRAPH_APP_ID, "resourceAccess": [
            {"id": "role-user", "type": "Role"}, {"id": "role-audit", "type": "Role"},
            {"id": "role-group", "type": "Role"}]}]
    assert graph.granted("sp-9") == ["role-group", "role-user"]
    assert not [call for call in graph.calls if call[1].endswith("/addPassword")]
    assert not (app["dir"] / ".env").exists()
    assert tenant["verified"] == [("tenant-1", "client-9", "old-secret", ["User.Read.All", "Group.Read.All"])]


def test_an_app_with_every_permission_changes_nothing_and_is_still_checked(app, tenant, monkeypatch, capsys):
    monkeypatch.setenv("PDT_AZURE_TENANT_ID", "tenant-1")
    monkeypatch.setenv("PDT_AZURE_CLIENT_ID", "client-9")
    monkeypatch.setenv("PDT_AZURE_CLIENT_SECRET", "old-secret")
    tenant["graph"] = graph = FakeGraph(
        apps=[{"id": "obj-9", "appId": "client-9", "displayName": "AutoIDM", "requiredResourceAccess": [
            {"resourceAppId": entra_app.GRAPH_APP_ID, "resourceAccess": [
                {"id": "role-user", "type": "Role"}, {"id": "role-group", "type": "Role"}]}]}],
        sps=[{"id": "sp-9", "appId": "client-9"}],
        assignments=[{"principalId": "sp-9", "resourceId": GRAPH_SP, "appRoleId": r}
                     for r in ("role-user", "role-group")])
    monkeypatch.setattr(entra_app.console, "confirm", lambda q: pytest.fail("asked to confirm"))

    assert entra_app.setup(app, assume_yes=False, device_code=False) == 0

    assert [call[0] for call in graph.calls] == ["GET"] * len(graph.calls)
    assert "already has every permission" in capsys.readouterr().out
    assert len(tenant["verified"]) == 1


def test_declining_changes_nothing(app, tenant, monkeypatch):
    monkeypatch.setattr(entra_app.console, "confirm", lambda q: False)

    assert entra_app.setup(app, assume_yes=False, device_code=False) == 1

    assert [call[0] for call in tenant["graph"].calls] == ["GET"] * len(tenant["graph"].calls)
    assert not (app["dir"] / ".env").exists()


def test_a_name_that_is_not_a_graph_application_permission_stops_before_any_change(
        project, tenant, monkeypatch, capsys):
    clear_azure_env(monkeypatch)
    add_app(project, "report", "entra:\n  graph_permissions: [User.Read.All, Mail.Send, Usr.Read.All]\n")

    assert entra_app.setup(config.merged_app("report"), assume_yes=True, device_code=False) == 1

    assert "not Microsoft Graph application permissions: Mail.Send, Usr.Read.All" in capsys.readouterr().out
    assert not tenant["graph"].apps


def test_a_client_id_the_tenant_does_not_know_is_an_error(app, tenant, monkeypatch, capsys):
    monkeypatch.setenv("PDT_AZURE_CLIENT_ID", "client-elsewhere")

    assert entra_app.setup(app, assume_yes=True, device_code=False) == 1

    assert "has no app registration with that client id" in capsys.readouterr().out


def test_a_new_service_principal_is_retried_until_entra_knows_the_app(app, tenant, monkeypatch):
    graph = tenant["graph"]
    real = graph.call
    failures = iter([True, True])

    def flaky(method, path, body=None):
        if (method, path) == ("POST", "/servicePrincipals") and next(failures, False):
            raise entra_app.EntraError("Microsoft Graph POST /servicePrincipals returned 400: no app")
        return real(method, path, body)
    monkeypatch.setattr(graph, "call", flaky)

    assert entra_app.setup(app, assume_yes=True, device_code=False) == 0

    assert graph.granted("sp-client-0") == ["role-group", "role-user"]


def test_the_entra_section_is_checked_and_merged(project):
    add_app(project, "report", "entra:\n  graph_permissions: User.Read.All\n  scopes: []\n")

    problems = config.validate()

    assert "report/config.yml: entra: unknown key 'scopes'" in problems
    assert any("graph_permissions must be a list" in p for p in problems)


def test_missing_azure_values_point_at_pdt_entra(app, monkeypatch):
    (app["dir"] / "config.yml").write_text(
        "entra:\n  graph_permissions: [User.Read.All]\n"
        "env:\n  required: [PDT_AZURE_TENANT_ID, PDT_AZURE_CLIENT_ID, PDT_AZURE_CLIENT_SECRET]\n")
    monkeypatch.delenv("PDT_ENV_SECRET_RESOURCE", raising=False)

    assert "run `pdt entra report`" in config.missing_env(config.merged_app("report"))


def test_pdt_run_offers_pdt_entra_and_runs_with_the_saved_values(app, monkeypatch):
    (app["dir"] / "config.yml").write_text(
        "entra:\n  graph_permissions: [User.Read.All]\nenv:\n  required: [PDT_AZURE_CLIENT_ID]\n")
    monkeypatch.delenv("PDT_ENV_SECRET_RESOURCE", raising=False)
    monkeypatch.setattr(cli, "can_prompt", lambda interactive: True)
    monkeypatch.setattr(cli.console, "confirm", lambda question: True)
    dispatched = []

    def dispatch(provider, command, name, assume_yes, extra=None):
        dispatched.append((provider, command, name))
        (app["dir"] / ".env").write_text("PDT_AZURE_CLIENT_ID=client-0\n")
        return 0
    monkeypatch.setattr(cli.deploy, "dispatch", dispatch)
    ran = []
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: ran.append(a) or
                        type("Done", (), {"returncode": 0})())

    args = cli.build_parser().parse_args(["run", "report"])
    assert args.func(args) == 0

    assert dispatched[-1] == ("azure", "entra", "report")
    assert ran


def test_pdt_run_without_a_terminal_does_not_offer(app, monkeypatch, capsys):
    (app["dir"] / "config.yml").write_text(
        "entra:\n  graph_permissions: [User.Read.All]\nenv:\n  required: [PDT_AZURE_CLIENT_ID]\n")
    monkeypatch.delenv("PDT_ENV_SECRET_RESOURCE", raising=False)
    monkeypatch.setattr(cli, "can_prompt", lambda interactive: False)
    monkeypatch.setattr(cli.deploy, "dispatch", lambda *a, **k: pytest.fail("offered pdt entra"))

    args = cli.build_parser().parse_args(["run", "report"])
    assert args.func(args) == 1
    assert "run `pdt entra report`" in capsys.readouterr().out


def test_pdt_entra_needs_an_entra_section(project, capsys):
    add_app(project, "report", "schedule: daily\n")

    args = cli.build_parser().parse_args(["entra", "report"])
    assert args.func(args) == 1
    assert "has no entra: section" in capsys.readouterr().out


def test_a_stopped_command_still_removes_the_admins_login(app, tenant, monkeypatch, tmp_path):
    folders = []

    def stopped(config_dir, *args):
        folders.append(Path(config_dir))
        tenant["stop"](15, None)
    monkeypatch.setattr(entra_app, "sign_in", stopped)

    with pytest.raises(SystemExit) as stop:
        entra_app.setup(app, assume_yes=True, device_code=False)

    assert stop.value.code == 143
    assert folders and not folders[0].exists()
