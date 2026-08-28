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
    assert any("apps list is only allowed in pdt.yml" in p for p in problems)


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
