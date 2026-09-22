from pdt import deploy_azure, deploy_azure_container_apps

SUBSCRIPTION = "11111111-1111-1111-1111-111111111111"


def settings_for(resource_group: str) -> dict[str, str]:
    return deploy_azure.azure_settings({"platform": {
        "provider": "azure", "subscription": SUBSCRIPTION, "resource_group": resource_group}})


def test_the_same_app_in_two_resource_groups_gets_two_job_names():
    one = deploy_azure_container_apps.job_name(settings_for("pdt"), "hello-world")
    two = deploy_azure_container_apps.job_name(settings_for("pdt-jon"), "hello-world")
    assert one != two
    assert one.startswith("pdt-hello-world-") and two.startswith("pdt-hello-world-")


def test_a_job_name_fits_the_azure_limit_and_keeps_the_suffix():
    settings = settings_for("pdt")
    name = deploy_azure_container_apps.job_name(settings, "salesforce-netsuite-customer-sync")
    assert len(name) <= 32
    assert name.endswith("-" + settings["suffix"][:7])


def test_the_legacy_name_is_what_pdt_used_before_the_suffix():
    assert deploy_azure_container_apps.legacy_job_name("hello-world") == "pdt-hello-world"
    legacy = deploy_azure_container_apps.legacy_job_name("salesforce-netsuite-customer-sync")
    assert legacy == "pdt-salesforce-netsuite-a60138e"


def test_find_job_falls_back_to_an_owned_legacy_job(monkeypatch):
    settings = settings_for("pdt")
    shown = {"pdt-hello-world": {"tags": {"managed-by": "pdt", "pdt-app": "hello-world"}}}
    monkeypatch.setattr(deploy_azure_container_apps, "az_json",
                        lambda *args: shown.get(args[4]))
    job, current = deploy_azure_container_apps.find_job(settings, "hello-world")
    assert job == "pdt-hello-world" and current is shown["pdt-hello-world"]

    shown["pdt-hello-world"]["tags"]["pdt-app"] = "other"
    job, current = deploy_azure_container_apps.find_job(settings, "hello-world")
    assert job == deploy_azure_container_apps.job_name(settings, "hello-world") and current is None
