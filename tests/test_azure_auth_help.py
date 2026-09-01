from types import SimpleNamespace

import pytest

from pdt import deploy_azure

AUTH_STDERR = (
    "(AuthorizationFailed) The client 'user@example.onmicrosoft.com' with "
    "object id '0000' does not have authorization to perform action "
    "'Microsoft.Resources/subscriptions/providers/read' over scope "
    "'/subscriptions/1111' or the scope is invalid. If access was recently "
    "granted, please refresh your credentials."
)


def install(monkeypatch, stderr):
    def fake_run(cmd, input=None, capture_output=True, text=True):
        return SimpleNamespace(returncode=1, stdout="", stderr=stderr)

    monkeypatch.setattr(deploy_azure.subprocess, "run", fake_run)


def test_authorization_failure_names_the_role(monkeypatch, capsys):
    install(monkeypatch, AUTH_STDERR)
    with pytest.raises(SystemExit):
        deploy_azure.run_quiet("group", "create")
    out = capsys.readouterr().out
    assert "Owner" in out
    assert "Active, Permanent" in out
    assert "Access management for Azure resources" in out
    assert "pdt login" in out
    assert "grant the role above" in out


def test_other_failures_stay_generic(monkeypatch, capsys):
    install(monkeypatch, "unrecognized arguments: --bogus")
    with pytest.raises(SystemExit):
        deploy_azure.run_quiet("group", "create")
    out = capsys.readouterr().out
    assert "Owner" not in out
    assert "fix the problem above" in out
