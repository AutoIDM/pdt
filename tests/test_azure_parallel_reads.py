"""The Azure plan reads Azure with resource manager and Key Vault GETs, all at once."""
import base64
import io
import json
import subprocess
import sys
import types
import urllib.error
import urllib.response

import pytest

from pdt import deploy_azure
from pdt import deploy_azure_container_apps as azure

SUBSCRIPTION = "11111111-1111-1111-1111-111111111111"
SETTINGS = {
    "subscription": SUBSCRIPTION, "resource_group": "pdt", "region": "eastus2",
    "registry": "pdtregistry", "environment": deploy_azure.Environment("pdt-shared", "pdt-eastus2", True),
    "identity": "pdt-runner", "workspace": "pdt-logs", "vault": "pdt-vault", "suffix": "abc1234def",
    "token": "arm-token",
}
JOB = azure.job_name(SETTINGS, "report")
LEGACY = azure.legacy_job_name("report")
STORE = deploy_azure.store_settings(SETTINGS)
SID = deploy_azure.secret_name("report")
MANAGED = {"tags": {"managed-by": "pdt", "pdt-app": "report"}}
# The end of each URL path the plan reads, and what Azure answers when the resource exists.
FOUND = {
    "/resourceGroups/pdt/resources": {"value": [{"type": "x", "name": "y"}]},
    "/resourceGroups/pdt": MANAGED,
    "/registries/pdtregistry": MANAGED,
    "/managedEnvironments/pdt-eastus2": MANAGED,
    "/userAssignedIdentities/pdt-runner": MANAGED,
    "/resourceGroups/pdt-shared": MANAGED,
    "/workspaces/pdt-logs": MANAGED,
    f"/jobs/{JOB}": MANAGED,
    f"/jobs/{JOB}/executions": {"value": []},
    f"/jobs/{LEGACY}": MANAGED,
    "/locks/pdt-report": {"name": "pdt-report"},
    "/locks/pdt-report-in-pdt": {"name": "pdt-report-in-pdt"},
    "/vaults/pdt-vault": MANAGED,
    f"/secrets/{SID}": {"value": "{}"},
    f"/secrets/{SID}/versions": {"value": [MANAGED]},
    "/resourceGroups/pdt-data": MANAGED,
    f"/storageAccounts/{STORE['account']}": MANAGED,
    f"/containers/{STORE['container']}": {"properties": {"metadata": {"managed_by": "pdt"}}},
}
DENIED = json.dumps({"error": {"code": "AuthorizationFailed", "message": "The client may not read it."}})


class Deployer:
    def usage(self):
        return 2, 2048


def opener(monkeypatch, status: dict[str, int] | None = None):
    """Answer each GET from FOUND, or with the HTTP status `status` names for its path end.

    Returns the requests made, as (path end, Authorization header).
    """
    requests = []

    def open_url(request, timeout):
        path = request.full_url.split("?")[0]
        end = max((key for key in FOUND if path.endswith(key)), key=len)
        requests.append((end, request.get_header("Authorization")))
        code = (status or {}).get(end, 200)
        if code != 200:
            body = DENIED if code == 403 else "{}"
            raise urllib.error.HTTPError(request.full_url, code, "refused", {}, io.BytesIO(body.encode()))
        return urllib.response.addinfourl(io.BytesIO(json.dumps(FOUND[end]).encode()), {},
                                          request.full_url, 200)

    monkeypatch.setattr(deploy_azure.READ_OPENER, "open", open_url)
    monkeypatch.setattr(azure, "price_list", lambda *args: ({}, "USD", ""))
    tokens = []
    monkeypatch.setattr(azure, "access_token", lambda resource="": tokens.append(resource) or "vault-token")
    return requests, tokens


def read(secret: bool = True) -> dict:
    locks = azure.shared_locks(SETTINGS, azure.lock_owner("report"))
    return azure.read_state(SETTINGS, JOB, LEGACY, SID, secret, locks, STORE, Deployer(), "USD")


def test_every_read_finds_what_exists(monkeypatch):
    requests, _ = opener(monkeypatch)
    state = read()
    for key in ("group", "registry", "environment", "identity", "shared_group", "workspace"):
        assert state[key] == MANAGED
    assert state["resources"] == [{"type": "x", "name": "y"}]
    assert state["job"] == (MANAGED, None, [])
    assert state["missing_locks"] == []
    assert state["secret"] == (MANAGED, {"value": "{}"}, [MANAGED])
    assert state["store"] == ((MANAGED, MANAGED, FOUND[f"/containers/{STORE['container']}"]), (2, 2048))
    assert f"/jobs/{LEGACY}" not in [end for end, _ in requests]


@pytest.mark.parametrize("end, missing", [
    ("/resourceGroups/pdt/resources", lambda s: s["resources"] is None),
    ("/resourceGroups/pdt", lambda s: s["group"] is None),
    ("/registries/pdtregistry", lambda s: s["registry"] is None),
    ("/managedEnvironments/pdt-eastus2", lambda s: s["environment"] is None),
    ("/userAssignedIdentities/pdt-runner", lambda s: s["identity"] is None),
    ("/resourceGroups/pdt-shared", lambda s: s["shared_group"] is None),
    ("/workspaces/pdt-logs", lambda s: s["workspace"] is None),
    (f"/jobs/{JOB}", lambda s: s["job"] == (None, MANAGED, [])),
    ("/locks/pdt-report", lambda s: [lock.name for lock in s["missing_locks"]] == ["pdt-report"]),
    ("/vaults/pdt-vault", lambda s: s["secret"] == (None, None, [])),
    (f"/secrets/{SID}", lambda s: s["secret"] == (MANAGED, None, [])),
    ("/resourceGroups/pdt-data", lambda s: s["store"][0][0] is None),
    (f"/storageAccounts/{STORE['account']}", lambda s: s["store"] == ((MANAGED, None, None), None)),
    (f"/containers/{STORE['container']}", lambda s: s["store"] == ((MANAGED, MANAGED, None), None)),
])
def test_a_missing_resource_reads_as_none(monkeypatch, end, missing):
    opener(monkeypatch, {end: 404})
    assert missing(read())


@pytest.mark.parametrize("end, command", [
    ("/resourceGroups/pdt/resources", ("resource", "list", "--resource-group", "pdt")),
    ("/resourceGroups/pdt", ("group", "show", "--name", "pdt")),
    ("/registries/pdtregistry", ("acr", "show", "--name", "pdtregistry")),
    ("/managedEnvironments/pdt-eastus2", ("containerapp", "env", "show", "--name")),
    ("/userAssignedIdentities/pdt-runner", ("identity", "show", "--name", "pdt-runner")),
    ("/resourceGroups/pdt-shared", ("group", "show", "--name", "pdt-shared")),
    ("/workspaces/pdt-logs", ("monitor", "log-analytics", "workspace", "show")),
    (f"/jobs/{JOB}", ("containerapp", "job", "show", "--name")),
    (f"/jobs/{JOB}/executions", ("containerapp", "job", "execution", "list")),
    ("/locks/pdt-report-in-pdt", ("lock", "show", "--name", "pdt-report-in-pdt")),
    ("/vaults/pdt-vault", ("keyvault", "show", "--name", "pdt-vault")),
    ("/resourceGroups/pdt-data", ("group", "show", "--name", "pdt-data")),
    (f"/storageAccounts/{STORE['account']}", ("storage", "account", "show", "--name")),
    (f"/containers/{STORE['container']}", ("storage", "container-rm", "show", "--storage-account")),
])
def test_a_refused_read_stops_the_plan_and_names_its_az_command(monkeypatch, end, command):
    opener(monkeypatch, {end: 403})
    with pytest.raises(deploy_azure.ReadError) as error:
        read()
    assert error.value.command[:4] == command
    assert str(error.value) == "ERROR: (AuthorizationFailed) The client may not read it."


@pytest.mark.parametrize("end", [f"/secrets/{SID}", f"/secrets/{SID}/versions"])
def test_a_vault_that_refuses_this_login_reads_as_no_secret(monkeypatch, end):
    """The deploy grants the login its Key Vault role before it writes the secret."""
    opener(monkeypatch, {end: 403})
    vault, current, versions = read()["secret"]
    assert vault == MANAGED
    assert versions == []
    assert current is None or end.endswith("/versions")


def test_a_read_error_prints_what_run_quiet_prints_for_the_same_az_command(monkeypatch, capsys):
    monkeypatch.setattr(deploy_azure, "az", lambda *args: subprocess.CompletedProcess(
        args, 1, "", "ERROR: (AuthorizationFailed) The client may not read it."))
    with pytest.raises(SystemExit):
        deploy_azure.run_quiet("group", "show", "--name", "pdt")
    from_az = capsys.readouterr().out

    opener(monkeypatch, {"/resourceGroups/pdt": 403})
    monkeypatch.setattr(sys, "argv", ["deploy_azure.py", "deploy", "report"])
    monkeypatch.setattr(deploy_azure, "load_app", lambda name: {})
    monkeypatch.setattr(deploy_azure, "run_command", lambda app, args: read())
    with pytest.raises(SystemExit) as exit_code:
        deploy_azure.main()
    assert exit_code.value.code == 1
    assert capsys.readouterr().out.endswith(from_az)


def test_every_read_uses_the_token_preflight_read_and_one_vault_token(monkeypatch):
    requests, tokens = opener(monkeypatch)
    read()
    assert tokens == [deploy_azure.VAULT_RESOURCE]
    for end, header in requests:
        assert header == ("Bearer vault-token" if end.startswith("/secrets/") else "Bearer arm-token")


def test_an_app_with_no_secret_reads_no_secret_and_asks_for_no_vault_token(monkeypatch):
    requests, tokens = opener(monkeypatch)
    assert read(secret=False)["secret"] == (MANAGED, None, [])
    assert tokens == []
    assert not any(end.startswith("/secrets/") for end, _ in requests)


def test_preflight_keeps_the_token_it_reads_the_principal_from(monkeypatch):
    account = {"id": SUBSCRIPTION, "name": "Azure", "state": "Enabled", "user": {"type": "user"}}
    claims = json.dumps({"oid": "deployer"}).encode()
    token = "x." + base64.urlsafe_b64encode(claims).decode().rstrip("=") + ".y"
    asked = []
    monkeypatch.delenv("PDT_AZURE_DEPLOYER_OBJECT_ID", raising=False)
    monkeypatch.setattr(deploy_azure, "can_prompt", lambda interactive: False)
    monkeypatch.setattr(deploy_azure, "az_json", lambda *args: account)
    monkeypatch.setattr(deploy_azure, "access_token", lambda resource="": asked.append(resource) or token)
    settings = deploy_azure.preflight({"platform": {}}, {"subscription": SUBSCRIPTION,
                                                         "resource_group": "pdt"})
    assert asked == [""]
    assert settings["token"] == token
    assert settings["deployer_object_id"] == "deployer"


def test_the_plan_store_holds_one_storage_token_and_starts_no_az(monkeypatch):
    for name in ("azure", "azure.core"):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    monkeypatch.setitem(sys.modules, "azure.core.credentials", types.SimpleNamespace(
        AccessToken=lambda token, expires_on: (token, expires_on)))
    token = "x." + base64.urlsafe_b64encode(b'{"exp": 1900000000}').decode().rstrip("=") + ".y"
    store = deploy_azure.deployer_store(SETTINGS, "report", token)
    assert store.credentials.get_token("https://storage.azure.com/.default") == (token, 1900000000)


def test_a_connect_with_no_answer_starts_again(monkeypatch):
    tries = []

    def connect(address, timeout, source_address=None):
        tries.append(timeout)
        if len(tries) == 1:
            raise TimeoutError
        return type("Socket", (), {"settimeout": lambda self, value: tries.append(value)})()

    monkeypatch.setattr(deploy_azure.socket, "create_connection", connect)
    deploy_azure.quick_connection(("management.azure.com", 443), 30)
    assert tries == [deploy_azure.CONNECT_SECONDS, deploy_azure.CONNECT_SECONDS, 30]
