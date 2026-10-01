import pytest

from conftest import add_app
from pdt.config import (PROJECT_FILE, ConfigError, find_apps, find_env_files,
                        find_project, merged_app, validate_app)


def test_finds_the_project_in_the_current_folder(project):
    assert find_project() == project


def test_walks_up_from_a_nested_folder(project, monkeypatch):
    deep = project / "some-app" / "sub" / "deeper"
    deep.mkdir(parents=True)
    monkeypatch.chdir(deep)
    assert find_project() == project


def test_an_app_pdt_yml_does_not_stop_the_walk(project, monkeypatch):
    app = add_app(project, "my-report", config_text="schedule: daily\n")
    monkeypatch.chdir(app)
    assert find_project() == project


def test_a_folder_below_an_app_still_finds_the_project(project, monkeypatch):
    app = add_app(project, "my-report", config_text="schedule: daily\n")
    deep = app / "transform" / "models"
    deep.mkdir(parents=True)
    monkeypatch.chdir(deep)
    assert find_project() == project


def test_an_app_folder_alone_is_not_a_project(tmp_path, monkeypatch):
    monkeypatch.delenv("PDT_PROJECT", raising=False)
    app = add_app(tmp_path, "my-report", config_text="schedule: daily\n")
    monkeypatch.chdir(app)
    with pytest.raises(ConfigError):
        find_project()


def test_pdt_project_pointing_at_an_app_folder_is_an_error(project, monkeypatch):
    app = add_app(project, "my-report", config_text="schedule: daily\n")
    monkeypatch.setenv("PDT_PROJECT", str(app))
    with pytest.raises(ConfigError):
        find_project()


def test_env_lookup_climbs_past_the_app_folder(project):
    app = add_app(project, "my-report", config_text="schedule: daily\n")
    (project / ".env").write_text("A=1\n")
    (app / ".env").write_text("B=2\n")
    assert find_env_files(app) == [app / ".env", project / ".env"]


def test_an_old_config_yml_is_reported_and_not_read(project):
    app = add_app(project, "my-report")
    (app / "config.yml").write_text("schedule: daily\n")
    assert validate_app("my-report") == [
        "my-report/config.yml: an app's settings now live in my-report/pdt.yml. Rename the "
        "file, and pin pdt-cli 0.1.2 or newer in my-report/run.py, because older versions "
        "read config.yml"]
    assert merged_app("my-report")["schedule"] is None


def test_reports_a_useful_error_outside_any_project(tmp_path, monkeypatch):
    monkeypatch.delenv("PDT_PROJECT", raising=False)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ConfigError) as caught:
        find_project()
    assert "pdt init" in str(caught.value)


def test_pdt_project_wins_over_the_working_folder(project, tmp_path, monkeypatch):
    other = tmp_path.parent / "other-project"
    other.mkdir()
    (other / PROJECT_FILE).write_text("platform: {}\n")
    monkeypatch.setenv("PDT_PROJECT", str(other))
    assert find_project() == other


def test_pdt_project_pointing_at_a_non_project_is_an_error(project, tmp_path, monkeypatch):
    monkeypatch.setenv("PDT_PROJECT", str(tmp_path.parent))
    with pytest.raises(ConfigError):
        find_project()


def test_find_apps_lists_only_folders_holding_run_py(project):
    add_app(project, "report-one")
    add_app(project, "report-two")
    (project / "notes").mkdir()
    (project / ".hidden").mkdir()
    (project / ".hidden" / "run.py").write_text("")
    assert find_apps() == ["report-one", "report-two"]


def test_find_apps_leaves_out_a_disabled_app(project):
    add_app(project, "report-one")
    add_app(project, "report-two")
    (project / "report-two" / "pdt.yml").write_text("enabled: false\n")
    assert find_apps() == ["report-one"]
