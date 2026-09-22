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


def test_the_grant_condition_of_an_app_with_no_read_list_is_the_released_string():
    released = (
        "((!(ActionMatches{'Microsoft.Storage/storageAccounts/blobServices/containers/blobs/read'} AND NOT SubOperationMatches{'Blob.List'})"
        " AND !(ActionMatches{'Microsoft.Storage/storageAccounts/blobServices/containers/blobs/write'})"
        " AND !(ActionMatches{'Microsoft.Storage/storageAccounts/blobServices/containers/blobs/delete'}))"
        " OR (@Resource[Microsoft.Storage/storageAccounts/blobServices/containers/blobs:path] StringStartsWith 'my-report/'))"
        " AND ((!(ActionMatches{'Microsoft.Storage/storageAccounts/blobServices/containers/blobs/read'} AND SubOperationMatches{'Blob.List'}))"
        " OR (@Request[Microsoft.Storage/storageAccounts/blobServices/containers/blobs:prefix] StringStartsWith 'my-report/'))")
    assert deploy_azure.store_condition("my-report") == released
    assert deploy_azure.store_condition("my-report", []) == released


def test_the_grant_condition_adds_read_and_list_of_each_named_folder():
    assert deploy_azure.store_condition("report", ["a", "b"]) == (
        "((!(ActionMatches{'Microsoft.Storage/storageAccounts/blobServices/containers/blobs/write'})"
        " AND !(ActionMatches{'Microsoft.Storage/storageAccounts/blobServices/containers/blobs/add/action'})"
        " AND !(ActionMatches{'Microsoft.Storage/storageAccounts/blobServices/containers/blobs/delete'}))"
        " OR (@Resource[Microsoft.Storage/storageAccounts/blobServices/containers/blobs:path] StringStartsWith 'report/'))"
        " AND ((!(ActionMatches{'Microsoft.Storage/storageAccounts/blobServices/containers/blobs/read'} AND NOT SubOperationMatches{'Blob.List'}))"
        " OR (@Resource[Microsoft.Storage/storageAccounts/blobServices/containers/blobs:path] StringStartsWith 'report/'"
        " OR @Resource[Microsoft.Storage/storageAccounts/blobServices/containers/blobs:path] StringStartsWith 'a/'"
        " OR @Resource[Microsoft.Storage/storageAccounts/blobServices/containers/blobs:path] StringStartsWith 'b/'))"
        " AND ((!(ActionMatches{'Microsoft.Storage/storageAccounts/blobServices/containers/blobs/read'} AND SubOperationMatches{'Blob.List'}))"
        " OR (@Request[Microsoft.Storage/storageAccounts/blobServices/containers/blobs:prefix] StringStartsWith 'report/'"
        " OR @Request[Microsoft.Storage/storageAccounts/blobServices/containers/blobs:prefix] StringStartsWith 'a/'"
        " OR @Request[Microsoft.Storage/storageAccounts/blobServices/containers/blobs:prefix] StringStartsWith 'b/'))")


def test_a_condition_longer_than_azure_allows_stops_the_deploy(capsys):
    names = [f"client-{number:02d}" for number in range(40)]
    deploy_azure.check_condition_size("report", names[:30])
    with pytest.raises(SystemExit):
        deploy_azure.check_condition_size("report", names)
    assert " ".join(capsys.readouterr().out.split()).endswith(
        "storage.read in report/pdt.yml names 40 apps. The access rule that names them is "
        "9976 bytes long, and Azure allows 8192 bytes. Remove names from the list.")


def test_plan_lines_show_each_folder_the_job_may_read():
    store = deploy_azure.store_settings(SETTINGS)
    lines = plain(deploy_azure.store_plan(store, True, "report", "pdt-runner", ["a", "b"]))
    where = f"storage account {store['account']}, container {store['container']}"
    assert lines[1:4] == [f"grant pdt-runner write access to report/ in {where}",
                          f"grant pdt-runner read access to a/ in {where}",
                          f"grant pdt-runner read access to b/ in {where}"]
    assert len(lines) == 5


def test_usage_is_unknown_until_the_data_role_lands():
    class Deployer:
        def usage(self):
            raise RuntimeError("ErrorCode:AuthorizationPermissionMismatch")

    assert deploy_azure.store_usage(Deployer()) is None


def test_another_storage_error_is_raised():
    class Deployer:
        def usage(self):
            raise RuntimeError("ErrorCode:ContainerNotFound")

    with pytest.raises(RuntimeError):
        deploy_azure.store_usage(Deployer())
