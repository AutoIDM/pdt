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


def test_destroy_does_not_purge_another_groups_legacy_vault(monkeypatch):
    settings = {"resource_group": "pdt-verify", "vault": "pdt-current",
                "legacy_vault": "pdt-other-group"}
    reads = []
    calls = []

    def deleted(*args):
        reads.append(args)
        return {"name": args[-1]}

    monkeypatch.setattr(deploy_azure, "az_json", deleted)
    monkeypatch.setattr(deploy_azure, "az_tsv", lambda *args: "false")
    monkeypatch.setattr(deploy_azure, "run_quiet",
                        lambda *args: calls.append(args))
    deploy_azure.destroy_group(settings)
    assert reads == [("keyvault", "show-deleted", "--name", "pdt-current")]
    assert calls == [
        ("group", "delete", "--name", "pdt-verify", "--yes"),
        ("keyvault", "purge", "--name", "pdt-current"),
    ]
