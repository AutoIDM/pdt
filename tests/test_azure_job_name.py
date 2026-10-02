import re

from pdt import deploy_azure
from pdt.deploy_azure import JOB_NAME_LIMIT, job_name, legacy_job_name

LONG = "salesforce-netsuite-opportunities"
VALID = re.compile(r"^[a-z][a-z0-9-]*[a-z0-9]$")


def test_short_app_name_is_used_as_is():
    assert job_name("hello-world") == "pdt-hello-world"
    assert legacy_job_name("hello-world") == ""


def test_long_app_name_keeps_its_end():
    name = job_name(LONG)
    assert len(name) <= JOB_NAME_LIMIT
    assert name.startswith("pdt-")
    assert "netsuite-opportunities" in name
    assert VALID.match(name)


def test_long_app_name_drops_whole_words_not_letters():
    name = job_name("salesforce-netsuite-invoice-sync")
    assert re.fullmatch(r"pdt-netsuite-invoice-sync-[0-9a-f]{4}", name)


def test_one_long_word_is_cut_to_fit():
    name = job_name("x" * 50)
    assert len(name) == JOB_NAME_LIMIT
    assert VALID.match(name)


def test_apps_with_the_same_ending_get_different_jobs():
    assert job_name(LONG) != job_name("hubspot-netsuite-opportunities")


def test_job_name_is_stable():
    assert job_name(LONG) == job_name(LONG)


def test_name_is_cleaned_before_it_is_cut():
    name = job_name("Salesforce NetSuite_Opportunities Sync!")
    assert len(name) <= JOB_NAME_LIMIT
    assert VALID.match(name)


def test_cut_never_starts_the_tail_with_a_hyphen():
    for length in range(30, 60):
        for words in range(2, 6):
            app = "-".join("x" * max(1, length // words) for _ in range(words))
            name = job_name(app)
            assert len(name) <= JOB_NAME_LIMIT
            assert VALID.match(name), name


def test_legacy_name_matches_what_older_deploys_created():
    legacy = legacy_job_name(LONG)
    assert re.fullmatch(r"pdt-salesforce-netsuite-[0-9a-f]{7}", legacy)
    assert legacy != job_name(LONG)


def test_legacy_job_is_only_the_apps_own(monkeypatch):
    calls = []

    def fake_show(*args):
        calls.append(args)
        return {"tags": {"managed-by": "pdt", "pdt-app": "someone-else"}}

    monkeypatch.setattr(deploy_azure, "az_json", fake_show)
    assert deploy_azure.legacy_job("pdt", LONG) == ("", None)
    assert calls and legacy_job_name(LONG) in calls[0]
    assert deploy_azure.legacy_job("pdt", "hello-world") == ("", None)
    assert len(calls) == 1
