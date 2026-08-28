import os
import tempfile
from pathlib import Path

import pytest
import yaml

from pdt import scaffold
from pdt.config import PROJECT_FILE, ConfigError, find_apps, validate_app

POSIX_SYSTEM_FOLDERS = ["/", "/usr", "/usr/local/bin", "/etc"]
WINDOWS_SYSTEM_FOLDERS = ["C:\\", "C:\\Windows", "C:\\Windows\\System32", "C:\\Program Files"]


def test_home_folder_is_flagged():
    assert "home folder" in scaffold.bad_place(Path.home().resolve())


@pytest.mark.parametrize(
    "folder", WINDOWS_SYSTEM_FOLDERS if os.name == "nt" else POSIX_SYSTEM_FOLDERS
)
def test_system_folders_are_flagged(folder):
    assert scaffold.bad_place(Path(folder)) != ""


def test_an_ordinary_folder_is_not_flagged():
    assert scaffold.bad_place(Path.home() / "projects" / "reports") == ""


def test_a_temporary_folder_is_flagged():
    assert scaffold.bad_place(Path(tempfile.gettempdir()) / "x") != ""


def test_named_target_must_not_exist(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "taken").mkdir()
    with pytest.raises(ConfigError):
        scaffold.choose_target("taken", assume_yes=True)


def test_named_target_is_resolved_against_the_working_folder(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert scaffold.choose_target("reports", assume_yes=True) == tmp_path / "reports"


def test_project_yaml_is_valid_and_carries_the_platform():
    text = scaffold.project_yaml({"provider": "aws", "region": "us-east-1"})
    data = yaml.safe_load(text)
    assert data["platform"] == {"provider": "aws", "region": "us-east-1"}
    assert data["apps"] == []


def test_project_yaml_without_a_platform_is_still_valid():
    data = yaml.safe_load(scaffold.project_yaml({}))
    assert "platform" not in data


def test_init_creates_a_usable_project(tmp_path, monkeypatch):
    monkeypatch.delenv("PDT_PROJECT", raising=False)
    monkeypatch.chdir(tmp_path)
    assert scaffold.init("reports", assume_yes=True) == 0
    root = tmp_path / "reports"
    assert (root / PROJECT_FILE).is_file()
    assert (root / ".gitignore").is_file()
    assert ".env" in (root / ".gitignore").read_text()
    monkeypatch.chdir(root)
    assert find_apps() == [scaffold.STARTER]


def test_init_on_an_existing_project_changes_nothing(project):
    before = (project / PROJECT_FILE).read_text()
    assert scaffold.init(None, assume_yes=True) == 0
    assert (project / PROJECT_FILE).read_text() == before


def test_every_bundled_example_is_complete():
    examples = sorted(p for p in scaffold.EXAMPLES.iterdir() if p.is_dir())
    assert examples, "the wheel ships no examples"
    for example in examples:
        assert (example / "run.py").is_file()
        config = yaml.safe_load((example / "config.yml").read_text())
        assert config["schedule"], f"{example.name} has no schedule"
        assert "name" not in config, f"{example.name} pins a name, so it cannot be renamed"


def test_new_app_copies_an_example_under_a_new_name(project):
    assert scaffold.new_app("my-report", "impossible-travel-report") == 0
    assert (project / "my-report" / "run.py").is_file()
    assert find_apps() == ["my-report"]
    assert not any("does not match directory" in p for p in validate_app("my-report"))


def test_new_app_pins_the_installed_version(project):
    from pdt import __version__
    scaffold.new_app("my-report", "impossible-travel-report")
    header = (project / "my-report" / "run.py").read_text()
    assert f"pdt-cli[apps]=={__version__}" in header
    assert "PDT_VERSION" not in header


def test_new_app_refuses_an_unknown_example(project):
    with pytest.raises(ConfigError):
        scaffold.new_app("my-report", "no-such-example")
    assert not (project / "my-report").exists()


def test_new_app_refuses_a_path_outside_the_examples_folder(project):
    with pytest.raises(ConfigError):
        scaffold.new_app("my-report", "../../etc")
    assert not (project / "my-report").exists()


def test_new_app_refuses_an_existing_folder(project):
    (project / "my-report").mkdir()
    with pytest.raises(ConfigError):
        scaffold.new_app("my-report", "impossible-travel-report")


def test_new_app_without_from_names_a_real_example(project, capsys):
    with pytest.raises(ConfigError) as caught:
        scaffold.new_app("my-report", None)
    listed = [example.name for example in scaffold.examples()]
    assert any(name in str(caught.value) for name in listed)
    assert listed[0] in capsys.readouterr().out
