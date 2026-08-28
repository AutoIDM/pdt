import pytest

from conftest import add_app
from pdt.config import PROJECT_FILE, ConfigError, find_apps, find_project


def test_finds_the_project_in_the_current_folder(project):
    assert find_project() == project


def test_walks_up_from_a_nested_folder(project, monkeypatch):
    deep = project / "some-app" / "sub" / "deeper"
    deep.mkdir(parents=True)
    monkeypatch.chdir(deep)
    assert find_project() == project


def test_an_app_config_does_not_stop_the_walk(project, monkeypatch):
    app = add_app(project, "my-report", config_text="schedule: daily\n")
    monkeypatch.chdir(app)
    assert find_project() == project


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
