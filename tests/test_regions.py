import os

import pytest
import yaml

from conftest import add_app
from pdt import regions
from pdt.config import merged_app


@pytest.fixture(autouse=True)
def no_region_env(monkeypatch, tmp_path):
    for names in regions.REGION_ENV.values():
        for name in names:
            monkeypatch.delenv(name, raising=False)
    for name in ("AWS_PROFILE", "CLOUDSDK_ACTIVE_CONFIG_NAME"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "aws-config"))
    monkeypatch.setenv("AZURE_CONFIG_DIR", str(tmp_path / "azure"))
    monkeypatch.setenv("CLOUDSDK_CONFIG", str(tmp_path / "gcloud"))


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


def write_cli_settings(tmp_path):
    (tmp_path / "aws-config").write_text("[default]\nregion = eu-west-1\n"
                                         "[profile work]\nregion = ap-south-1\n")
    (tmp_path / "azure").mkdir()
    (tmp_path / "azure" / "config").write_text("[defaults]\nlocation = northeurope\n")
    (tmp_path / "gcloud" / "configurations").mkdir(parents=True)
    (tmp_path / "gcloud" / "active_config").write_text("work\n")
    (tmp_path / "gcloud" / "configurations" / "config_work").write_text(
        "[compute]\nregion = europe-west4\n[run]\nregion = europe-west1\n")


def test_the_time_zone_comes_before_the_cli_settings(tmp_path):
    write_cli_settings(tmp_path)
    for provider, region in zip(regions.PROVIDERS, ("eu-west-2", "uksouth", "europe-west2")):
        assert regions.region_suggestion(provider, {}, "Europe/London") == (
            region, "this computer's time zone is Europe/London")


def test_the_cli_settings_come_before_the_default(tmp_path):
    write_cli_settings(tmp_path)
    found = {p: regions.region_suggestion(p, {}, "Etc/UTC") for p in regions.PROVIDERS}
    assert found["aws"][0] == "eu-west-1" and "AWS CLI profile default" in found["aws"][1]
    assert found["azure"][0] == "northeurope" and "defaults.location" in found["azure"][1]
    assert found["google-cloud"][0] == "europe-west1" and "run/region" in found["google-cloud"][1]


def test_the_aws_settings_follow_the_profile(tmp_path):
    write_cli_settings(tmp_path)
    assert regions.region_suggestion("aws", {"profile": "work"}, "Etc/UTC")[0] == "ap-south-1"


def test_gcloud_falls_back_to_the_compute_region(tmp_path):
    write_cli_settings(tmp_path)
    (tmp_path / "gcloud" / "configurations" / "config_work").write_text(
        "[compute]\nregion = europe-west4\n")
    assert regions.region_suggestion("google-cloud", {}, "")[0] == "europe-west4"


def test_with_no_place_and_no_settings_the_default_says_so():
    region, reason = regions.region_suggestion("azure", {}, "Etc/UTC")
    assert region == "eastus2"
    assert reason.startswith("it is pdt's default")


def test_choose_region_names_the_cli_settings(project, monkeypatch, tmp_path, capsys):
    write_cli_settings(tmp_path)
    (project / "pdt.yml").write_text("platform:\n  provider: azure\n")
    add_app(project, "my-report", "schedule: daily\n")
    monkeypatch.setattr(regions, "local_timezone", lambda: "Etc/UTC")
    assert regions.choose_region(merged_app("my-report"), "azure", True) == ""
    assert "northeurope, because defaults.location" in capsys.readouterr().out
