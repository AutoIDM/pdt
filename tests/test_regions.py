import os

import pytest
import yaml

from conftest import add_app
from pdt import regions
from pdt.config import merged_app


@pytest.fixture(autouse=True)
def no_region_env(monkeypatch):
    for names in regions.REGION_ENV.values():
        for name in names:
            monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize("zone,expected", [
    ("Europe/London", ("eu-west-2", "uksouth", "europe-west2")),
    ("America/Los_Angeles", ("us-west-2", "westus2", "us-west1")),
    ("America/New_York", ("us-east-1", "eastus2", "us-east4")),
    ("Australia/Sydney", ("ap-southeast-2", "australiaeast", "australia-southeast1")),
    # A zone the table does not list falls back to its continent.
    ("Europe/Budapest", ("eu-central-1", "germanywestcentral", "europe-west3")),
    # A zone that names no place keeps the old defaults.
    ("Etc/UTC", ("us-east-1", "eastus2", "us-central1")),
    ("", ("us-east-1", "eastus2", "us-central1")),
])
def test_suggests_the_nearest_region_of_each_provider(zone, expected):
    assert tuple(regions.suggest_region(p, zone) for p in regions.PROVIDERS) == expected


def test_every_country_has_a_region_for_every_provider():
    for country, names in regions.COUNTRY_REGIONS.items():
        assert len(names) == len(regions.PROVIDERS), country
    for zone, country in regions.ZONE_COUNTRY.items():
        assert country in regions.COUNTRY_REGIONS, zone


def test_every_windows_zone_names_a_place_or_utc():
    for windows_id, zone in regions.WINDOWS_ZONES.items():
        assert zone == "Etc/UTC" or regions.country(zone) != "", windows_id


def test_a_uk_windows_computer_gets_uk_south():
    zone = regions.WINDOWS_ZONES["GMT Standard Time"]
    assert regions.suggest_region("azure", zone) == "uksouth"


@pytest.mark.skipif(os.name == "nt", reason="Windows reads the registry, not TZ")
def test_reads_the_tz_variable(monkeypatch):
    monkeypatch.setenv("TZ", "Europe/London")
    assert regions.local_timezone() == "Europe/London"


def test_question_names_the_provider():
    assert regions.question("google-cloud") == "Which Google Cloud region should hold your jobs?"


def region_in(path):
    return (yaml.safe_load(path.read_text()).get("platform") or {}).get("region")


def test_yes_takes_the_suggestion_and_saves_it(project, monkeypatch):
    add_app(project, "my-report")
    monkeypatch.setattr(regions, "local_timezone", lambda: "Europe/London")
    assert regions.choose_region(merged_app("my-report"), "azure", True) == ""
    assert region_in(project / "pdt.yml") == "uksouth"


def test_the_user_can_pick_another_region(project, monkeypatch):
    add_app(project, "my-report")
    monkeypatch.setattr(regions, "local_timezone", lambda: "Europe/London")
    monkeypatch.setattr(regions, "can_prompt", lambda _interactive: True)
    monkeypatch.setattr(regions.console, "ask", lambda _question, _default: "ukwest")
    assert regions.choose_region(merged_app("my-report"), "azure", False) == ""
    assert region_in(project / "pdt.yml") == "ukwest"


def test_an_empty_answer_takes_the_suggestion(project, monkeypatch):
    add_app(project, "my-report")
    monkeypatch.setattr(regions, "local_timezone", lambda: "Europe/London")
    monkeypatch.setattr(regions, "can_prompt", lambda _interactive: True)
    monkeypatch.setattr(regions.console, "ask", lambda _question, _default: "")
    assert regions.choose_region(merged_app("my-report"), "azure", False) == ""
    assert region_in(project / "pdt.yml") == "uksouth"


def test_no_one_to_ask_and_no_yes_names_the_fix(project, monkeypatch):
    add_app(project, "my-report")
    monkeypatch.setattr(regions, "local_timezone", lambda: "Europe/London")
    monkeypatch.setattr(regions, "can_prompt", lambda _interactive: False)
    problem = regions.choose_region(merged_app("my-report"), "azure", False)
    assert "region: uksouth" in problem and "pdt.yml" in problem and "--yes" in problem
    assert region_in(project / "pdt.yml") is None


def test_a_set_region_is_never_asked_again(project, monkeypatch):
    (project / "pdt.yml").write_text("platform:\n  provider: azure\n  region: westeurope\n")
    add_app(project, "my-report")
    monkeypatch.setattr(regions, "can_prompt", lambda _interactive: pytest.fail("asked"))
    assert regions.choose_region(merged_app("my-report"), "azure", False) == ""
    assert region_in(project / "pdt.yml") == "westeurope"


def test_a_region_in_the_environment_is_not_asked_or_saved(project, monkeypatch):
    add_app(project, "my-report")
    monkeypatch.setenv("PDT_AZURE_REGION", "westeurope")
    assert regions.choose_region(merged_app("my-report"), "azure", False) == ""
    assert region_in(project / "pdt.yml") is None


def test_windows_has_no_region(project):
    (project / "pdt.yml").write_text("platform:\n  provider: windows\n")
    add_app(project, "my-report")
    assert regions.choose_region(merged_app("my-report"), "windows", False) == ""
    assert region_in(project / "pdt.yml") is None
