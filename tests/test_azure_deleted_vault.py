import pytest

from pdt import deploy_azure

SETTINGS = {"vault": "pdt-abc"}


def recorded(monkeypatch, deleted):
    calls = []
    monkeypatch.setattr(deploy_azure, "az_json", lambda *args: deleted)
    monkeypatch.setattr(deploy_azure, "run_quiet",
                        lambda *args, **kwargs: calls.append(args) or "")
    return calls


def test_no_deleted_vault_means_nothing_to_purge(monkeypatch):
    calls = recorded(monkeypatch, None)
    deploy_azure.purge_deleted_vault(SETTINGS)
    assert calls == []


def test_a_pdt_vault_in_soft_delete_is_purged(monkeypatch):
    calls = recorded(monkeypatch, {"properties": {"tags": {"managed-by": "pdt"}}})
    deploy_azure.purge_deleted_vault(SETTINGS)
    assert calls == [("keyvault", "purge", "--name", "pdt-abc")]


def test_someone_elses_deleted_vault_is_left_alone(monkeypatch):
    calls = recorded(monkeypatch, {"properties": {"tags": {}}})
    with pytest.raises(SystemExit):
        deploy_azure.purge_deleted_vault(SETTINGS)
    assert calls == []
