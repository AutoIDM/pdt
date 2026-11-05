import io
import subprocess

import pytest

from pdt import deploy_azure
from pdt.deploy_common import FatalAPIError

SETTINGS = {"vault": "pdt-abc", "region": "eastus", "subscription": "s", "token": "t"}
REFUSED = ("ERROR: (AuthorizationFailed) The client does not have authorization to perform action "
           "'Microsoft.KeyVault/locations/deletedVaults/read'")


def recorded(monkeypatch, deleted):
    calls = []
    monkeypatch.setattr(deploy_azure, "deleted_vault", lambda settings: deleted)
    monkeypatch.setattr(deploy_azure, "run_quiet",
                        lambda *args, **kwargs: calls.append(args) or "")
    return calls


def test_one_wait_covers_the_secret_delete_the_poll_and_the_purge(monkeypatch, waits):
    seen = []
    monkeypatch.setattr(deploy_azure, "az_json",
                        lambda *args: seen.append((args[2], list(waits))) or {"name": "s1"})
    monkeypatch.setattr(deploy_azure, "run_quiet",
                        lambda *args, **kwargs: seen.append((args[2], list(waits))) or "")
    deploy_azure.purge_secret(SETTINGS, "s1")
    wait = ["deleting: Key Vault secret [bold not dim]s1[/]"]
    assert seen == [("delete", wait), ("show-deleted", wait), ("purge", wait)]
    assert waits == []


def test_no_deleted_vault_means_nothing_to_purge(monkeypatch):
    calls = recorded(monkeypatch, None)
    deploy_azure.purge_deleted_vault(SETTINGS)
    assert calls == []


def test_a_pdt_vault_in_soft_delete_is_purged(monkeypatch):
    calls = recorded(monkeypatch, {"properties": {"tags": {"managed-by": "pdt"}}})
    deploy_azure.purge_deleted_vault(SETTINGS)
    assert calls == [("keyvault", "purge", "--name", "pdt-abc", "--location", "eastus")]


def test_someone_elses_deleted_vault_is_left_alone(monkeypatch):
    calls = recorded(monkeypatch, {"properties": {"tags": {}}})
    with pytest.raises(SystemExit):
        deploy_azure.purge_deleted_vault(SETTINGS)
    assert calls == []


def test_destroy_does_not_purge_another_groups_legacy_vault(monkeypatch):
    settings = {"resource_group": "pdt-verify", "vault": "pdt-current", "region": "eastus",
                "legacy_vault": "pdt-other-group"}
    reads = []
    calls = []

    def deleted(*args):
        if args[:2] == ("resource", "list"):
            return []
        return {"name": "pdt-verify"}

    monkeypatch.setattr(deploy_azure, "az_json", deleted)
    monkeypatch.setattr(deploy_azure, "deleted_vault",
                        lambda settings: reads.append(settings["vault"]) or {"name": settings["vault"]})
    monkeypatch.setattr(deploy_azure, "az_tsv", lambda *args: "false")
    monkeypatch.setattr(deploy_azure, "run_quiet",
                        lambda *args, **kwargs: calls.append(args))
    monkeypatch.setattr(deploy_azure, "delete_unless_locked",
                        lambda *args, **kwargs: calls.append(args) or "")
    assert deploy_azure.destroy_group(settings, "report") is True
    assert reads == ["pdt-current"]
    assert calls == [
        ("group", "delete", "--name", "pdt-verify", "--yes"),
        ("keyvault", "purge", "--name", "pdt-current", "--location", "eastus"),
    ]


def test_a_refused_read_of_the_deleted_vault_fails_with_the_azure_message(monkeypatch):
    response = io.BytesIO(b'{"error": {"code": "AuthorizationFailed", "message": "no right"}}')
    response.status, response.reason = 403, "Forbidden"

    def refused(*args, **kwargs):
        raise FatalAPIError("refused", response)

    monkeypatch.setattr(deploy_azure, "fetch_json", refused)
    monkeypatch.setattr(deploy_azure, "run_quiet", lambda *args, **kwargs: pytest.fail("purged"))
    with pytest.raises(deploy_azure.ReadError, match="AuthorizationFailed"):
        deploy_azure.purge_deleted_vault(SETTINGS)


def test_a_refused_az_read_fails_and_names_the_azure_message(monkeypatch, capsys):
    monkeypatch.setattr(deploy_azure, "az", lambda *args: subprocess.CompletedProcess(args, 1, "", REFUSED))
    with pytest.raises(SystemExit):
        deploy_azure.az_json("resource", "list", "--resource-group", "pdt")
    assert "deletedVaults/read" in capsys.readouterr().out
    assert deploy_azure.az_json("resource", "list", refused_is_missing=True) is None


def test_a_missing_resource_is_still_none(monkeypatch):
    missing = "ERROR: (ResourceNotFound) The Resource was not found."
    monkeypatch.setattr(deploy_azure, "az", lambda *args: subprocess.CompletedProcess(args, 3, "", missing))
    assert deploy_azure.az_json("resource", "show", "--name", "x") is None
