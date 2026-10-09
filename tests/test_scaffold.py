import os
import tempfile
from pathlib import Path

import pytest
import yaml

from pdt import __version__, powershell, scaffold
from pdt.config import APP_FILE, PROJECT_FILE, ConfigError, find_apps, powershell_scripts, validate_app

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


def test_named_target_may_already_be_a_folder(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "taken").mkdir()
    assert scaffold.choose_target("taken", assume_yes=True) == tmp_path / "taken"


def test_the_current_folder_is_a_valid_named_target(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert scaffold.choose_target(".", assume_yes=True) == tmp_path


def test_named_target_must_not_be_a_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "notes.txt").write_text("")
    with pytest.raises(ConfigError):
        scaffold.choose_target("notes.txt", assume_yes=True)


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


def test_init_writes_agent_docs(tmp_path, monkeypatch):
    monkeypatch.delenv("PDT_PROJECT", raising=False)
    monkeypatch.chdir(tmp_path)
    assert scaffold.init(None, assume_yes=True) == 0
    agents = (tmp_path / "AGENTS.md").read_text()
    assert "pdt validate" in agents
    assert (tmp_path / "CLAUDE.md").read_text().strip() == "@AGENTS.md"


def test_init_keeps_the_users_own_agent_docs(tmp_path, monkeypatch):
    monkeypatch.delenv("PDT_PROJECT", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "AGENTS.md").write_text("mine\n")
    assert scaffold.init(None, assume_yes=True) == 0
    assert (tmp_path / "AGENTS.md").read_text() == "mine\n"
    assert (tmp_path / "CLAUDE.md").read_text().strip() == "@AGENTS.md"


def test_init_on_an_existing_project_changes_nothing(project):
    before = (project / PROJECT_FILE).read_text()
    assert scaffold.init(None, assume_yes=True) == 0
    assert (project / PROJECT_FILE).read_text() == before


def test_every_bundled_example_is_complete():
    examples = sorted(p for p in scaffold.EXAMPLES.iterdir() if p.is_dir())
    assert examples, "the wheel ships no examples"
    for example in examples:
        assert (example / "run.py").is_file() or any(example.glob("*.ps1")), example.name
        config = yaml.safe_load((example / "config.yml").read_text())
        assert config["schedule"], f"{example.name} has no schedule"
        assert "name" not in config, f"{example.name} pins a name, so it cannot be renamed"


def test_every_example_summary_is_whole(tmp_path):
    # The summary is the whole comment block at the top of config.yml, not
    # only its first line, so a summary that wraps is not cut short.
    for example in scaffold.examples():
        assert scaffold.summary_of(example).endswith("."), example.name
    app = tmp_path / "wrapped"
    app.mkdir()
    (app / APP_FILE).write_text("# One line.\n# And a second.\n\nschedule: daily\n")
    assert scaffold.summary_of(app) == "One line. And a second."


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


def test_new_app_copies_a_powershell_example(project, capsys):
    assert scaffold.new_app("my-report", "powershell-report") == 0
    assert (project / "my-report" / "report.ps1").is_file()
    assert not (project / "my-report" / "run.py").exists()
    assert find_apps() == ["my-report"]
    assert "my-report/report.ps1" in capsys.readouterr().out


def test_new_app_copies_an_examples_data_files(project):
    assert scaffold.new_app("license-audit", "azure-license-waste") == 0
    app = project / "license-audit"
    assert (app / "LicenseWasteAudit.ps1").is_file()
    assert (app / "license-prices.csv").read_text().startswith("SkuPartNumber,")
    assert find_apps() == ["license-audit"]


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


@pytest.fixture
def scripts_app(project, monkeypatch):
    folder = project / "ad-report"
    folder.mkdir()
    (folder / "report.ps1").write_text("Write-Output hi\n")
    scans = []

    def scan(app, provider):
        scans.append((app["name"], provider))
        return powershell.ScriptScan(["report.ps1"], [], [
            powershell.ModuleNeed("ImportExcel", "7.8.10", "#Requires in report.ps1"),
            powershell.ModuleNeed("Microsoft.Graph.Users", None, "command Get-MgUser"),
            powershell.ModuleNeed("not.on.the.gallery", None, "using module in report.ps1")], [])

    monkeypatch.setattr(powershell, "scan", scan)
    monkeypatch.setattr(powershell, "latest_versions", lambda names: {
        "Microsoft.Graph.Users": "2.25.0", "not.on.the.gallery": "latest"})
    return folder, scans


def test_from_scripts_writes_requirements_psd1_and_run_py(scripts_app, capsys):
    folder, scans = scripts_app
    assert scaffold.from_scripts("ad-report") == 0
    assert scans == [("ad-report", "azure")]
    assert (folder / "requirements.psd1").read_text() == (
        "# pdt installs these modules before the scripts run. "
        "`pdt new ad-report --from-scripts` wrote this file.\n"
        "@{\n"
        "    'ImportExcel' = '7.8.10'\n"
        "    'Microsoft.Graph.Users' = '2.25.0'\n"
        "    'not.on.the.gallery' = 'latest'\n"
        "}\n")
    run_py = (folder / "run.py").read_text()
    assert f'"pdt-cli[apps]=={__version__}"' in run_py
    assert "PDT_VERSION" not in run_py
    assert find_apps() == ["ad-report"]
    out = capsys.readouterr().out
    assert "ad-report/requirements.psd1" in out and "pdt run ad-report" in out


def test_from_scripts_keeps_the_ps1_files_a_powershell_app(scripts_app):
    folder, _scans = scripts_app
    scaffold.from_scripts("ad-report")
    assert powershell_scripts(folder) == ["report.ps1"]


@pytest.mark.parametrize("existing", ["run.py", "requirements.psd1"])
def test_from_scripts_refuses_to_overwrite(scripts_app, existing):
    folder, scans = scripts_app
    (folder / existing).write_text("")
    with pytest.raises(ConfigError, match=f"ad-report/{existing} already exists. Delete it first"):
        scaffold.from_scripts("ad-report")
    assert scans == []


def test_from_scripts_needs_an_existing_folder_in_the_project(scripts_app):
    with pytest.raises(ConfigError, match="there is no folder no-such-app/"):
        scaffold.from_scripts("no-such-app")
    with pytest.raises(ConfigError, match="not an app name"):
        scaffold.from_scripts("../ad-report")


def test_from_scripts_needs_a_ps1_file(scripts_app, project):
    (project / "notes").mkdir()
    (project / "notes" / "readme.txt").write_text("")
    with pytest.raises(ConfigError, match="notes/ holds no .ps1 file"):
        scaffold.from_scripts("notes")
