from pdt import deploy_azure

GROUP = {"name": "pdt", "tags": {"managed-by": "pdt"}}
OWNED = {"name": "pdt-x", "type": "Microsoft.KeyVault/vaults", "tags": {"managed-by": "pdt"}}
SMART = {"name": deploy_azure.SMART_ACTION_GROUP, "type": "microsoft.insights/actiongroups", "tags": None}
STRANGER = {"name": "theirs", "type": "microsoft.insights/actiongroups", "tags": None}


def listing(monkeypatch, resources):
    monkeypatch.setattr(deploy_azure, "az_json",
                        lambda *args: GROUP if args[0] == "group" else resources)


def test_the_smart_detection_group_azure_made_does_not_block_deletion(monkeypatch):
    listing(monkeypatch, [OWNED, SMART])
    assert deploy_azure.group_can_be_deleted({"resource_group": "pdt"}, [])


def test_another_untagged_resource_still_blocks_deletion(monkeypatch):
    listing(monkeypatch, [OWNED, STRANGER])
    assert not deploy_azure.group_can_be_deleted({"resource_group": "pdt"}, [])
