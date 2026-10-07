import re

import pytest

from pdt import deploy_azure, deploy_azure_container_apps

SUBSCRIPTION = "11111111-1111-1111-1111-111111111111"


def settings_for(resource_group: str) -> dict[str, str]:
    return deploy_azure.azure_settings({"platform": {
        "provider": "azure", "subscription": SUBSCRIPTION, "resource_group": resource_group}})


# Container Apps job names: lowercase letters, digits, and single hyphens, a letter
# first, a letter or digit last, at most 32 characters (az containerapp job create --name).
AZURE_JOB_NAME = re.compile(r"^(?=.{2,32}$)[a-z](?!.*--)[a-z0-9-]*[a-z0-9]$")

EXAMPLES = [
    ("hello-world", "pdt-hello-world-"),
    ("2024-q3-report", "pdt-2024-q3-report-"),
    ("salesforce-netsuite-accounts", "pdt-slsfrc-ntst-accnts-"),
    ("salesforce-netsuite-opportunities", "pdt-slsfrc-ntst-opprtnts-"),
    ("hubspot-netsuite-opportunities", "pdt-hbspt-ntst-opprtnts-"),
    ("salesforce-netsuite-invoice-sync", "pdt-ntst-invc-sync-"),
    ("workday-to-active-directory-user-provisioning", "pdt-drctry-usr-prvsnng-"),
    ("netsuitetimesheetexportnightly", "pdt-ntsttmshtxprtnghtly-"),
    ("abcdefghijklmnopqrstu", "pdt-abcdfghjklmnpqrst-"),
    ("aeiou", "pdt-aeiou-"),
    ("aeiou-aeiou-aeiou-aeiou-aeiou", "pdt-a-a-a-a-a-"),
    ("a" * 60, "pdt-a-"),
    ("Sales Force_Sync!!", "pdt-sales-force-sync-"),
    ("___", "pdt-"),
]


@pytest.mark.parametrize(("app", "start"), EXAMPLES)
def test_a_job_name_is_valid_for_azure_and_keeps_a_readable_app_part(app, start):
    name = deploy_azure_container_apps.job_name(settings_for("pdt"), app)
    assert AZURE_JOB_NAME.match(name), name
    assert name.startswith(start) and re.fullmatch(r"[0-9a-f]{7}", name[len(start):])
    assert name == deploy_azure_container_apps.job_name(settings_for("pdt"), app)


def test_every_example_gets_its_own_job_name():
    names = {deploy_azure_container_apps.job_name(settings_for("pdt"), app) for app, _ in EXAMPLES}
    assert len(names) == len(EXAMPLES)


def test_apps_that_shorten_to_the_same_words_differ_by_the_hash():
    one = deploy_azure_container_apps.job_name(settings_for("pdt"), "salesforce-netsuite-sync")
    two = deploy_azure_container_apps.job_name(settings_for("pdt"), "slesforce-netsuite-sync")
    assert one[:-7] == two[:-7] and one != two


def test_the_same_app_in_two_resource_groups_gets_two_job_names():
    one = deploy_azure_container_apps.job_name(settings_for("pdt"), "hello-world")
    two = deploy_azure_container_apps.job_name(settings_for("pdt-jon"), "hello-world")
    assert one != two
    assert one.startswith("pdt-hello-world-") and two.startswith("pdt-hello-world-")


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
