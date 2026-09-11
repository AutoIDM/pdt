import hashlib

from pdt import deploy_azure

SUBSCRIPTION = "11111111-1111-1111-1111-111111111111"


def settings_for(resource_group: str) -> dict[str, str]:
    return deploy_azure.azure_settings({"platform": {
        "provider": "azure", "subscription": SUBSCRIPTION, "resource_group": resource_group}})


def test_two_projects_in_one_subscription_get_different_shared_names():
    one, two = settings_for("pdt"), settings_for("pdt-verify")
    for key in ("vault", "storage", "registry", "suffix"):
        assert one[key] != two[key], key


def test_the_legacy_vault_name_is_the_subscription_only_hash():
    legacy = hashlib.sha256(SUBSCRIPTION.encode()).hexdigest()[:10]
    assert settings_for("pdt-verify")["legacy_vault"] == f"pdt-{legacy}"


def test_without_a_subscription_the_resource_group_seeds_both_names():
    settings = deploy_azure.azure_settings({"platform": {"provider": "azure", "resource_group": "pdt"}})
    assert settings["legacy_vault"] == settings["vault"]
