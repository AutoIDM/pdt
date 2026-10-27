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
