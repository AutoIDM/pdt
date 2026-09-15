from types import SimpleNamespace

import pytest

from pdt import deploy_azure

NO_ROLE_STDERR = (
    "(AuthorizationFailed) The client 'user@example.onmicrosoft.com' with "
    "object id '0000' does not have authorization to perform action "
    "'Microsoft.Resources/subscriptions/providers/read' over scope "
    "'/subscriptions/1111' or the scope is invalid. If access was recently "
    "granted, please refresh your credentials."
)

CANNOT_GRANT_STDERR = (
    "(AuthorizationFailed) The client 'user@example.onmicrosoft.com' with "
    "object id '0000' does not have authorization to perform action "
    "'Microsoft.Authorization/roleAssignments/write' over scope "
    "'/subscriptions/1111/resourceGroups/pdt/providers/Microsoft.KeyVault"
    "/vaults/pdt-kv' or the scope is invalid."
)


def install(monkeypatch, stderr):
    def fake_run(cmd, input=None, capture_output=True, text=True):
        return SimpleNamespace(returncode=1, stdout="", stderr=stderr)

    monkeypatch.setattr(deploy_azure.subprocess, "run", fake_run)


def test_denied_action_is_read_from_the_error():
    assert deploy_azure.denied_action(CANNOT_GRANT_STDERR) == (
        "Microsoft.Authorization/roleAssignments/write")
    assert deploy_azure.denied_action("unrecognized arguments: --bogus") == ""


def test_no_role_names_both_roles(monkeypatch, capsys):
    install(monkeypatch, NO_ROLE_STDERR)
    with pytest.raises(SystemExit):
        deploy_azure.run_quiet("group", "create")
    out = capsys.readouterr().out
    assert "holds no role on the subscription" in out
    assert "Contributor and Role Based Access Control Administrator" in out
    assert "Active and Permanent" in out
    assert "Access management for Azure resources" in out
    assert "pdt login" in out
    assert "grant the roles above" in out


def test_cannot_grant_names_the_grant_role_only(monkeypatch, capsys):
    install(monkeypatch, CANNOT_GRANT_STDERR)
    with pytest.raises(SystemExit):
        deploy_azure.run_quiet("role", "assignment", "create")
    out = capsys.readouterr().out
    assert "cannot grant roles" in out
    assert "Role Based Access Control Administrator" in out
    assert "Contributor and" not in out
    assert "grant the roles above" in out


def test_owner_is_never_required(monkeypatch, capsys):
    install(monkeypatch, NO_ROLE_STDERR)
    with pytest.raises(SystemExit):
        deploy_azure.run_quiet("group", "create")
    out = capsys.readouterr().out
    assert "needs the Owner" not in out
    assert "Owner also works" in out


def test_other_failures_stay_generic(monkeypatch, capsys):
    install(monkeypatch, "unrecognized arguments: --bogus")
    with pytest.raises(SystemExit):
        deploy_azure.run_quiet("group", "create")
    out = capsys.readouterr().out
    assert "Role Based Access Control Administrator" not in out
    assert "fix the problem above" in out
