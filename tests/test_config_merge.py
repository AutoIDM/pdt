import pytest

from conftest import add_app
from pdt.config import ConfigError, merged_app, storage_read, validate, validate_app

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


def test_a_byte_order_mark_from_windows_powershell_is_not_part_of_the_first_key(project):
    (project / "pdt.yml").write_bytes(b"\xef\xbb\xbf" + "platform:\n  provider: windows\n"
                                      "apps: []\n".encode())
    folder = add_app(project, "my-report")
    (folder / "pdt.yml").write_bytes(
        b"\xef\xbb\xbf" + "schedule: daily\nconfig:\n  greeting: Grüße\n".encode())
    assert validate_app("my-report") == []
    app = merged_app("my-report")
    assert app["platform"]["provider"] == "windows"
    assert app["config"]["greeting"] == "Grüße"


def test_unknown_app_is_an_error(project):
    with pytest.raises(ConfigError):
        merged_app("nope")


def test_apps_list_is_rejected_inside_an_app_folder(project):
    add_app(project, "my-report", "apps:\n  - name: x\n")
    problems = validate_app("my-report")
    assert any("belongs in the top level of the project's pdt.yml" in p for p in problems)


def test_name_must_match_the_folder(project):
    add_app(project, "my-report", "name: other\nschedule: daily\n")
    assert any("does not match directory" in p for p in validate_app("my-report"))


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
    assert validate_app("my-report") == [
        "my-report/pdt.yml: storage must be true, false, or a map with a read: list of app names"]


def test_storage_read_names_other_apps(project):
    add_app(project, "client1")
    add_app(project, "client2")
    add_app(project, "report", "storage:\n  read:\n    - client1\n    - client2\n")
    assert validate_app("report") == []
    assert storage_read(merged_app("report")) == ["client1", "client2"]
    assert storage_read(merged_app("client1")) == []


def test_storage_read_must_be_a_list_of_app_names(project):
    add_app(project, "report", "storage:\n  read: client1\n")
    assert validate_app("report") == [
        "report/pdt.yml: storage.read must be a list of app names"]
    add_app(project, "report", "storage:\n  read: [1]\n")
    assert validate_app("report") == [
        "report/pdt.yml: storage.read must be a list of app names"]
    add_app(project, "report", "storage:\n  write: [client1]\n  read: []\n")
    assert validate_app("report") == ["report/pdt.yml: storage: unknown key 'write'"]


def test_storage_read_names_an_app_of_the_project_and_not_itself(project):
    add_app(project, "report", "storage:\n  read: [report, missing]\n")
    assert validate_app("report") == [
        "report/pdt.yml: storage.read names 'report', which is this app; "
        "an app already reads its own folder, so remove the name",
        "report/pdt.yml: storage.read names 'missing', which is not an app of this project"]


def test_storage_read_works_with_azure_only(project):
    add_app(project, "client1")
    add_app(project, "report", "platform:\n  provider: windows\nstorage:\n  read: [client1]\n")
    assert validate_app("report") == [
        "report/pdt.yml: storage.read works with the azure provider only, "
        "and this app uses windows; remove the key"]


def test_storage_read_needs_the_read_key(project):
    add_app(project, "report", "storage: {}\n")
    assert validate_app("report") == [
        "report/pdt.yml: storage.read must be a list of app names"]
    add_app(project, "report", "storage:\n  read: []\n")
    assert validate_app("report") == []
    assert storage_read(merged_app("report")) == []


def test_storage_read_names_each_app_once(project):
    add_app(project, "client1")
    add_app(project, "report", "storage:\n  read: [client1, client1]\n")
    assert validate_app("report") == [
        "report/pdt.yml: storage.read names 'client1' more than once; keep one"]


def test_storage_read_names_an_app_with_a_folder_in_the_azure_store(project):
    add_app(project, "client1", "storage: false\n")
    add_app(project, "client2", "platform:\n  provider: windows\n")
    add_app(project, "report", "storage:\n  read: [client1, client2]\n")
    assert validate_app("report") == [
        "report/pdt.yml: storage.read names 'client1', which has storage: false, "
        "so it has no folder to read",
        "report/pdt.yml: storage.read names 'client2', which uses the windows provider, "
        "so its folder is not in the Azure store"]


def test_windows_app_with_no_timezone_runs_on_local_time(project):
    (project / "pdt.yml").write_text("platform:\n  provider: windows\n")
    add_app(project, "my-report", "schedule: daily\n")
    assert merged_app("my-report")["timezone"] == "local"


def test_cloud_app_with_no_timezone_runs_on_utc(project):
    (project / "pdt.yml").write_text("platform:\n  provider: google-cloud\n  region: us-central1\n")
    add_app(project, "my-report", "schedule: daily\n")
    assert merged_app("my-report")["timezone"] == "Etc/UTC"


def test_explicit_timezone_on_the_apps_entry_wins_on_a_cloud_provider(project):
    (project / "pdt.yml").write_text(
        "platform:\n  provider: google-cloud\n  region: us-central1\n"
        "apps:\n  - name: my-report\n    timezone: America/Chicago\n")
    add_app(project, "my-report", "schedule: daily\n")
    assert merged_app("my-report")["timezone"] == "America/Chicago"


def test_windows_app_runs_on_local_time_whatever_timezone_it_sets(project):
    (project / "pdt.yml").write_text(
        "platform:\n  provider: windows\n  timezone: America/Chicago\n"
        "apps:\n  - name: my-report\n    timezone: Europe/London\n")
    add_app(project, "my-report", "schedule: daily\ntimezone: Asia/Tokyo\n")
    assert merged_app("my-report")["timezone"] == "local"
    assert validate_app("my-report") == []


def test_app_that_picks_windows_itself_defaults_to_local_time(project):
    (project / "pdt.yml").write_text("platform:\n  provider: google-cloud\n  region: us-central1\n")
    add_app(project, "my-report", "schedule: daily\nplatform:\n  provider: windows\n")
    assert merged_app("my-report")["timezone"] == "local"


def test_platform_timezone_in_pdt_yml_reaches_an_app_without_one(project):
    (project / "pdt.yml").write_text(
        "platform:\n  provider: google-cloud\n  region: us-central1\n  timezone: America/Chicago\n")
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
        "pdt.yml: platform: 'schedule' belongs in the top level of the app's pdt.yml, "
        "or its apps: entry in the project's pdt.yml, not here"]
