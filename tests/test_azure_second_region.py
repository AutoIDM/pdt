import pytest

from pdt import deploy_azure, deploy_azure_container_apps

SUBSCRIPTION = "11111111-1111-1111-1111-111111111111"
GROUP_IN_EASTUS2 = {"location": "eastus2", "tags": {"managed-by": "pdt"}}


def westus2_settings() -> dict:
    return {
        "subscription": SUBSCRIPTION, "resource_group": "pdt", "region": "westus2",
        "vault": "pdt-vault", "workspace": "pdt-logs", "deployer_object_id": "d",
        "deployer_principal_type": "User",
        "environment": deploy_azure.Environment("pdt-shared", "pdt-westus2", True),
    }


@pytest.fixture
def azure(monkeypatch):
    groups = {}
    writes = []

    def read(*args):
        if args[:2] == ("group", "show"):
            return groups.get(args[args.index("--name") + 1])
        return None

    def write(*args, **kwargs):
        writes.append(args)
        return ""

    for module in (deploy_azure, deploy_azure_container_apps):
        monkeypatch.setattr(module, "az_json", read)
        monkeypatch.setattr(module, "run_quiet", write)
    monkeypatch.setattr(deploy_azure, "register_providers", lambda names: None)
    monkeypatch.setattr(deploy_azure, "assign_role", lambda *args, **kwargs: None)
    return groups, writes


def group_creates(writes) -> list[tuple[str, str]]:
    return [(args[args.index("--name") + 1], args[args.index("--location") + 1])
            for args in writes if args[:2] == ("group", "create")]


def ensure_every_group(settings: dict) -> None:
    deploy_azure.ensure_group_and_vault(settings, (), True)
    deploy_azure_container_apps.ensure_environment(settings, False, False)
    deploy_azure.ensure_store(settings, deploy_azure.store_settings(settings), False)


@pytest.mark.parametrize("group", ["pdt", "pdt-shared", "pdt-data"])
def test_a_second_region_deploy_leaves_an_existing_group_in_its_first_region(azure, group):
    groups, writes = azure
    groups[group] = GROUP_IN_EASTUS2
    ensure_every_group(westus2_settings())
    assert group not in dict(group_creates(writes))


def test_a_first_deploy_creates_each_group_in_the_app_region(azure):
    _, writes = azure
    ensure_every_group(westus2_settings())
    assert group_creates(writes) == [
        ("pdt", "westus2"), ("pdt-shared", "westus2"), ("pdt-data", "westus2")]
