import yaml

from conftest import add_app
from pdt.config import merged_app, save_app_key, validate_app

COMMENTED = """\
# My report.
schedule: daily  # every night
config:
  lookback_hours: 24
"""


def test_pause_is_off_unless_set(project):
    add_app(project, "my-report", "schedule: daily\n")
    assert merged_app("my-report")["pause"] is False


def test_the_app_file_beats_the_root_entry(project):
    (project / "pdt.yml").write_text(
        "platform:\n  provider: azure\napps:\n  - name: my-report\n    pause: true\n")
    add_app(project, "my-report")
    assert merged_app("my-report")["pause"] is True
    (project / "my-report" / "config.yml").write_text("pause: false\n")
    assert merged_app("my-report")["pause"] is False


def test_pause_must_be_a_bool(project):
    add_app(project, "my-report", "pause: yes please\n")
    assert validate_app("my-report") == ["my-report/config.yml: pause must be true or false"]


def test_save_app_key_appends_a_new_key_and_keeps_comments(project):
    add_app(project, "my-report", COMMENTED)
    saved = save_app_key(merged_app("my-report"), "pause", True)
    assert saved == project / "my-report" / "config.yml"
    text = saved.read_text()
    assert "# My report." in text
    assert "schedule: daily  # every night" in text
    assert text.endswith("pause: true\n")
    assert yaml.safe_load(text)["pause"] is True


def test_save_app_key_replaces_a_key_that_is_already_there(project):
    add_app(project, "my-report", "pause: true\nschedule: daily\n")
    saved = save_app_key(merged_app("my-report"), "pause", False)
    assert saved.read_text() == "pause: false\nschedule: daily\n"


def test_save_app_key_leaves_a_nested_key_with_the_same_name_alone(project):
    add_app(project, "my-report", "config:\n  pause: 5\n")
    save_app_key(merged_app("my-report"), "pause", True)
    loaded = yaml.safe_load((project / "my-report" / "config.yml").read_text())
    assert loaded == {"config": {"pause": 5}, "pause": True}


def test_save_app_key_creates_a_missing_config_file(project):
    add_app(project, "my-report")
    saved = save_app_key(merged_app("my-report"), "pause", True)
    assert saved.read_text() == "pause: true\n"
    assert merged_app("my-report")["pause"] is True
