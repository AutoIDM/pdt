import pytest

from pdt import deploy_azure

GROUP = {"name": "pdt", "tags": {"managed-by": "pdt"}}
OWNED = {"name": "pdt-x", "type": "Microsoft.KeyVault/vaults", "tags": {"managed-by": "pdt"}}
SMART = {"name": deploy_azure.SMART_ACTION_GROUP, "type": "microsoft.insights/actiongroups", "tags": None}
STRANGER = {"name": "theirs", "type": "microsoft.insights/actiongroups", "tags": None}


def test_the_smart_detection_group_azure_made_does_not_block_deletion():
    assert deploy_azure.group_can_be_deleted(GROUP, [OWNED, SMART], [])


def test_another_untagged_resource_still_blocks_deletion():
    assert not deploy_azure.group_can_be_deleted(GROUP, [OWNED, STRANGER], [])


def test_another_app_or_a_group_pdt_did_not_make_blocks_deletion():
    assert not deploy_azure.group_can_be_deleted(GROUP, [OWNED], ["other-app"])
    assert not deploy_azure.group_can_be_deleted({"name": "pdt", "tags": {}}, [OWNED], [])
    assert not deploy_azure.group_can_be_deleted(GROUP, None, [])


def test_destroy_empties_a_group_the_grant_made_and_keeps_the_group_and_its_identity(
        monkeypatch):
    granted = {"name": "pdt", "tags": {"managed-by": "pdt", "pdt-lifecycle": "retain"}}
    tagged = {"managed-by": "pdt"}
    resources = [
        {"id": "acr", "name": "pdtabc", "type": "Microsoft.ContainerRegistry/registries",
         "tags": tagged},
        {"id": "grant", "name": deploy_azure.GRANT_NAME,
         "type": "Microsoft.ManagedIdentity/userAssignedIdentities", "tags": tagged},
        {"id": "runner", "name": "pdt-runner",
         "type": "Microsoft.ManagedIdentity/userAssignedIdentities", "tags": tagged},
        {**OWNED, "id": "vault"},
        {"id": "job", "name": "pdt-hr", "type": "Microsoft.App/jobs",
         "tags": {**tagged, "pdt-app": "hr"}},
    ]
    reads = {("resource", "list"): resources, ("group", "show"): granted,
             ("keyvault", "show-deleted"): {"name": "pdt-x"}}
    deleted, purged, listed = [], [], []
    monkeypatch.setattr(deploy_azure, "az_json", lambda *args: reads.get(args[:2]))
    monkeypatch.setattr(deploy_azure, "delete_unless_locked",
                        lambda *args, **kwargs: deleted.append(args) and "")
    monkeypatch.setattr(deploy_azure, "run_quiet",
                        lambda *args, **kwargs: purged.append(args[:2]) or "")
    monkeypatch.setattr(deploy_azure, "list_remaining", listed.append)
    settings = {"resource_group": "pdt", "vault": "pdt-x"}
    assert deploy_azure.destroy_group(settings, "hr") is True
    assert [args[3] for args in deleted] == ["acr", "job", "runner", "vault"]
    assert all(args[:2] == ("resource", "delete") for args in deleted)
    assert purged == [("keyvault", "purge")]
    assert listed == ["pdt"]


def test_destroy_deletes_nothing_when_it_cannot_read_the_group(monkeypatch):
    resources = [{"id": "job", "name": "pdt-hr", "type": "Microsoft.App/jobs",
                  "tags": {"managed-by": "pdt", "pdt-app": "hr"}}]
    deleted = []
    monkeypatch.setattr(deploy_azure, "az_json",
                        lambda *args: resources if args[:2] == ("resource", "list") else None)
    monkeypatch.setattr(deploy_azure, "delete_unless_locked",
                        lambda *args, **kwargs: deleted.append(args) and "")
    with pytest.raises(SystemExit):
        deploy_azure.destroy_group({"resource_group": "pdt", "vault": "pdt-x"}, "hr")
    assert deleted == []


def test_a_lock_on_one_resource_of_a_retained_group_stops_the_shared_deletes(monkeypatch):
    granted = {"name": "pdt", "tags": {"managed-by": "pdt", "pdt-lifecycle": "retain"}}
    tagged = {"managed-by": "pdt"}
    resources = [
        {"id": "acr", "name": "pdtabc", "type": "Microsoft.ContainerRegistry/registries",
         "tags": tagged},
        {**OWNED, "id": "vault"},
        {"id": "job", "name": "pdt-hr", "type": "Microsoft.App/jobs",
         "tags": {**tagged, "pdt-app": "hr"}},
    ]
    reads = {("resource", "list"): resources, ("group", "show"): granted}
    deleted = []

    def delete(*args, **kwargs):
        deleted.append(args[3])
        return "pdt-other" if args[3] == "acr" else ""

    monkeypatch.setattr(deploy_azure, "az_json", lambda *args: reads.get(args[:2]))
    monkeypatch.setattr(deploy_azure, "delete_unless_locked", delete)
    assert deploy_azure.destroy_group({"resource_group": "pdt", "vault": "pdt-x"}, "hr") is False
    assert deleted == ["acr"]
