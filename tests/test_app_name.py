import pytest

from conftest import add_app
from pdt import config, scaffold
from pdt.config import ConfigError


@pytest.fixture
def nested_project(tmp_path, monkeypatch):
    monkeypatch.delenv("PDT_PROJECT", raising=False)
    project = tmp_path / "project"
    project.mkdir()
    (project / "pdt.yml").write_text("platform:\n  provider: azure\n")
    monkeypatch.chdir(project)
    return project


@pytest.mark.parametrize("name", ["hello-world", "report_2"])
def test_a_folder_name_is_an_app_name(name):
    assert config.app_name_problem(name) == ""


@pytest.mark.parametrize("name", ["..", "a/b", "a\\b", "../x", "", "."])
def test_a_path_is_not_an_app_name(name):
    assert config.app_name_problem(name) == (
        f"{name!r} is not an app name; use one folder name with no path separators")


def test_merged_app_rejects_a_name_outside_the_project(nested_project):
    add_app(nested_project.parent, "x")
    before = sorted(nested_project.parent.rglob("*"))
    with pytest.raises(ConfigError, match="is not an app name"):
        config.merged_app("../x")
    assert sorted(nested_project.parent.rglob("*")) == before


def test_new_app_rejects_a_name_outside_the_project(nested_project):
    before = sorted(nested_project.parent.rglob("*"))
    with pytest.raises(ConfigError, match="is not an app name"):
        scaffold.new_app("../x", "hello-world")
    assert sorted(nested_project.parent.rglob("*")) == before
