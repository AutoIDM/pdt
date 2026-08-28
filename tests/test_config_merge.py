import pytest

from conftest import add_app
from pdt.config import ConfigError, merged_app, validate, validate_app

ROOT_YAML = """\
platform:
  provider: azure
  region: eastus2
apps:
  - name: my-report
    schedule: hourly
    platform:
      region: westus2
    config:
      email_to: root@example.com
      lookback_hours: 24
"""


def test_app_config_beats_the_root_entry(project):
    (project / "pdt.yml").write_text(ROOT_YAML)
    add_app(project, "my-report", "platform:\n  region: centralus\nconfig:\n  lookback_hours: 72\n")
    app = merged_app("my-report")
    assert app["platform"]["region"] == "centralus"
    assert app["platform"]["provider"] == "azure"
    assert app["config"]["lookback_hours"] == 72
    assert app["config"]["email_to"] == "root@example.com"


def test_root_entry_beats_the_root_platform_defaults(project):
    (project / "pdt.yml").write_text(ROOT_YAML)
    add_app(project, "my-report")
    assert merged_app("my-report")["platform"]["region"] == "westus2"


def test_app_host_beats_the_root_platform_default(project):
    (project / "pdt.yml").write_text(
        "platform:\n  provider: windows\n  host: jobs-01\n")
    add_app(project, "my-report", "platform:\n  host: jobs-02\n")
    assert merged_app("my-report")["platform"]["host"] == "jobs-02"


def test_an_empty_host_is_rejected(project):
    (project / "pdt.yml").write_text("platform:\n  provider: windows\n  host: ''\n")
    add_app(project, "my-report")
    assert any("platform.host must be a non-empty string" in problem
               for problem in validate_app("my-report"))


def test_a_host_url_is_rejected(project):
    (project / "pdt.yml").write_text(
        "platform:\n  provider: windows\n  host: http://jobs-01\n")
    add_app(project, "my-report")
    assert any("without a URL scheme" in problem
               for problem in validate_app("my-report"))


def test_environment_variable_beats_every_file(project, monkeypatch):
    (project / "pdt.yml").write_text(ROOT_YAML)
    add_app(project, "my-report", "config:\n  lookback_hours: 72\n")
    monkeypatch.setenv("PDT_MY_REPORT_LOOKBACK_HOURS", "8")
    assert merged_app("my-report")["config"]["lookback_hours"] == "8"


def test_schedule_comes_from_the_app_file_when_both_set_it(project):
    (project / "pdt.yml").write_text(ROOT_YAML)
    add_app(project, "my-report", "schedule: daily\n")
    assert merged_app("my-report")["schedule"] == "daily"


def test_unknown_app_is_an_error(project):
    with pytest.raises(ConfigError):
        merged_app("nope")


def test_apps_list_is_rejected_inside_an_app_folder(project):
    add_app(project, "my-report", "apps:\n  - name: x\n")
    problems = validate_app("my-report")
    assert any("belongs in the top level of pdt.yml" in p for p in problems)


def test_name_must_match_the_folder(project):
    add_app(project, "my-report", "name: other\nschedule: daily\n")
    assert any("does not match directory" in p for p in validate_app("my-report"))


def test_aws_rejects_an_account_that_is_not_twelve_digits(project):
    (project / "pdt.yml").write_text("platform:\n  provider: aws\n  account: nope\n")
    add_app(project, "my-report", "schedule: daily\n")
    assert any("platform.account" in p for p in validate_app("my-report"))


def test_a_complete_project_validates_clean(project):
    (project / "pdt.yml").write_text(ROOT_YAML)
    add_app(project, "my-report")
    assert validate() == []


def test_storage_defaults_to_true(project):
    add_app(project, "my-report", "schedule: daily\n")
    assert merged_app("my-report")["storage"] is True


def test_storage_can_be_turned_off(project):
    add_app(project, "my-report", "schedule: daily\nstorage: false\n")
    assert merged_app("my-report")["storage"] is False


def test_storage_must_be_a_bool(project):
    add_app(project, "my-report", "schedule: daily\nstorage: yes-please\n")
    assert any("storage must be true or false" in p for p in validate_app("my-report"))


def test_windows_app_with_no_timezone_runs_on_local_time(project):
    (project / "pdt.yml").write_text("platform:\n  provider: windows\n")
    add_app(project, "my-report", "schedule: daily\n")
    assert merged_app("my-report")["timezone"] == "local"


def test_cloud_app_with_no_timezone_runs_on_utc(project):
    (project / "pdt.yml").write_text("platform:\n  provider: google-cloud\n  region: us-central1\n")
    add_app(project, "my-report", "schedule: daily\n")
    assert merged_app("my-report")["timezone"] == "Etc/UTC"


@pytest.mark.parametrize("provider", ["windows", "google-cloud"])
def test_explicit_timezone_on_the_apps_entry_wins_on_every_provider(project, provider):
    (project / "pdt.yml").write_text(
        f"platform:\n  provider: {provider}\n"
        "apps:\n  - name: my-report\n    timezone: America/Chicago\n")
    add_app(project, "my-report", "schedule: daily\n")
    assert merged_app("my-report")["timezone"] == "America/Chicago"


def test_app_that_picks_windows_itself_defaults_to_local_time(project):
    (project / "pdt.yml").write_text("platform:\n  provider: google-cloud\n  region: us-central1\n")
    add_app(project, "my-report", "schedule: daily\nplatform:\n  provider: windows\n")
    assert merged_app("my-report")["timezone"] == "local"


def test_platform_timezone_in_pdt_yml_reaches_an_app_without_one(project):
    (project / "pdt.yml").write_text("platform:\n  provider: windows\n  timezone: America/Chicago\n")
    add_app(project, "my-report", "schedule: daily\n")
    assert merged_app("my-report")["timezone"] == "America/Chicago"


def test_apps_entry_timezone_beats_platform_timezone(project):
    (project / "pdt.yml").write_text(
        "platform:\n  provider: google-cloud\n  region: us-central1\n  timezone: America/Chicago\n"
        "apps:\n  - name: my-report\n    timezone: Europe/London\n")
    add_app(project, "my-report", "schedule: daily\n")
    assert merged_app("my-report")["timezone"] == "Europe/London"


def test_app_config_timezone_beats_the_apps_entry_and_platform(project):
    (project / "pdt.yml").write_text(
        "platform:\n  provider: google-cloud\n  region: us-central1\n  timezone: America/Chicago\n"
        "apps:\n  - name: my-report\n    timezone: Europe/London\n")
    add_app(project, "my-report", "schedule: daily\ntimezone: Asia/Tokyo\n")
    assert merged_app("my-report")["timezone"] == "Asia/Tokyo"


def test_validate_accepts_timezone_under_platform_and_rejects_schedule(project):
    (project / "pdt.yml").write_text(
        "platform:\n  provider: google-cloud\n  region: us-central1\n  timezone: America/Chicago\n")
    add_app(project, "my-report", "schedule: daily\n")
    assert validate() == []
    (project / "pdt.yml").write_text(
        "platform:\n  provider: google-cloud\n  region: us-central1\n  schedule: daily\n")
    assert validate() == [
        "pdt.yml: platform: 'schedule' belongs in the top level of the app's config.yml, "
        "or its apps: entry in pdt.yml, not here"]
