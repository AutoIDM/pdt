import pytest
from conftest import plain
from pdt import deploy_azure
from pdt.deploy_common import store_name, store_suffix

SUBSCRIPTION = "11111111-1111-1111-1111-111111111111"
SETTINGS = {"subscription": SUBSCRIPTION, "region": "eastus", "resource_group": "pdt"}
BLOBS = "Microsoft.Storage/storageAccounts/blobServices/containers/blobs"


def test_store_names_derive_from_the_subscription():
    store = deploy_azure.store_settings(SETTINGS)
    suffix = store_suffix(SUBSCRIPTION)
    assert store["group"] == "pdt-data"
    assert store["account"] == f"pdtdata{suffix}"
    assert store["container"] == store_name(SUBSCRIPTION)
    assert len(store["account"]) <= 24
    assert store["account"].islower() and "-" not in store["account"]


def test_container_resource_id_sits_in_the_data_group():
    store = deploy_azure.store_settings(SETTINGS)
    assert store["container_id"] == (
        f"/subscriptions/{SUBSCRIPTION}/resourceGroups/pdt-data/providers"
        f"/Microsoft.Storage/storageAccounts/{store['account']}"
        f"/blobServices/default/containers/{store['container']}")


def test_store_url_names_the_app_folder():
    store = deploy_azure.store_settings(SETTINGS)
    url = deploy_azure.store_url(store, "my-report")
    assert url == (f"abfs://{store['container']}@{store['account']}"
                   ".dfs.core.windows.net/my-report/")
    assert url.endswith("/")


def test_plan_lines_use_the_shared_words():
    store = deploy_azure.store_settings(SETTINGS)
    create, grant, deployer = plain(deploy_azure.store_plan(store, False, "my-report", "pdt-runner"))
    assert create == (f"create storage account {store['account']}, container "
                      f"{store['container']} (kept after destroy)")
    assert grant == (f"grant pdt-runner write access to my-report/ in storage account "
                     f"{store['account']}, container {store['container']}")
    assert deployer.startswith("grant the signed-in Azure account")
    assert deploy_azure.store_plan(store, True, "my-report", "pdt-runner")[0].startswith(
        "use existing ")


def test_the_grant_condition_limits_reads_writes_and_deletes_to_the_app_folder():
    condition = deploy_azure.store_condition("my-report")
    assert condition == (
        f"((!(ActionMatches{{'{BLOBS}/read'}} AND NOT SubOperationMatches{{'Blob.List'}})"
        f" AND !(ActionMatches{{'{BLOBS}/write'}})"
        f" AND !(ActionMatches{{'{BLOBS}/delete'}}))"
        f" OR (@Resource[{BLOBS}:path] StringStartsWith 'my-report/'))"
        f" AND ((!(ActionMatches{{'{BLOBS}/read'}} AND SubOperationMatches{{'Blob.List'}}))"
        f" OR (@Request[{BLOBS}:prefix] StringStartsWith 'my-report/'))")


def test_the_grant_condition_names_one_app_only():
    assert "my-report/" in deploy_azure.store_condition("my-report")
    assert "other/" not in deploy_azure.store_condition("my-report")
    assert deploy_azure.store_condition("a") != deploy_azure.store_condition("b")


def test_usage_is_unknown_until_the_data_role_lands(capsys):
    class Deployer:
        def usage(self):
            raise RuntimeError("ErrorCode:AuthorizationPermissionMismatch")

    assert deploy_azure.store_usage(Deployer()) is None
    assert "cannot read the data store yet" in capsys.readouterr().out


def test_another_storage_error_is_raised():
    class Deployer:
        def usage(self):
            raise RuntimeError("ErrorCode:ContainerNotFound")

    with pytest.raises(RuntimeError):
        deploy_azure.store_usage(Deployer())
