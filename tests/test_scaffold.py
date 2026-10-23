import io
import os
import sys
import tempfile
from pathlib import Path

import pytest
import yaml

from conftest import add_app
from pdt import __version__, powershell, scaffold, watch
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


def test_init_starts_with_the_art_and_ends_with_the_discord_link(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("PDT_PROJECT", raising=False)
    monkeypatch.chdir(tmp_path)
    assert scaffold.init(None, assume_yes=True) == 0
    out = capsys.readouterr().out
    assert out.startswith(scaffold.ART)
    assert out.rstrip().endswith(f"{watch.COMMUNITY} {watch.SUPPORT_URL}")


def test_welcome_shows_the_art_the_first_commands_and_the_discord_link(capsys):
    assert scaffold.welcome() == 0
    out = capsys.readouterr().out
    assert scaffold.ART in out
    assert "pdt init my-project" in out
    assert f"pdt run {scaffold.STARTER}" in out
    assert out.rstrip().endswith(f"{watch.COMMUNITY} {watch.SUPPORT_URL}")


def test_a_cp1252_stdout_leaves_out_the_art(monkeypatch):
    stdout = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
    monkeypatch.setattr(sys, "stdout", stdout)
    assert scaffold.welcome() == 0
    stdout.flush()
    out = stdout.buffer.getvalue().decode("cp1252")
    assert "pdt init my-project" in out
    assert out.rstrip().endswith(f"{watch.COMMUNITY} {watch.SUPPORT_URL}")


def test_a_stdout_with_no_encoding_still_prints_the_welcome(monkeypatch):
    class Writer:
        def __init__(self):
            self.text = ""

        def write(self, text):
            self.text += text

        def flush(self):
            pass

    stdout = Writer()
    monkeypatch.setattr(sys, "stdout", stdout)
    assert scaffold.welcome() == 0
    assert stdout.text.rstrip().endswith(f"{watch.COMMUNITY} {watch.SUPPORT_URL}")


def test_the_art_fits_a_narrow_console():
    assert set(scaffold.ART) - set("█═║╔╗╚╝·°") <= {chr(code) for code in range(128)}
    assert scaffold.ART.encode("cp437").decode("cp437") == scaffold.ART
    assert max(len(line) for line in scaffold.ART.splitlines()) <= 60


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


VERIFY_RUN_PY = Path(__file__).resolve().parent.parent / "verify" / "scripts" / "templates" / "run.py.txt"


def test_every_run_py_header_installs_the_apps_extra():
    headers = {str(path): path.read_text() for path in scaffold.EXAMPLES.glob("*/run.py")}
    headers["scaffold.RUN_PY_TEXT"] = scaffold.RUN_PY_TEXT
    headers[str(VERIFY_RUN_PY)] = VERIFY_RUN_PY.read_text()
    assert [name for name, text in headers.items() if '"pdt-cli[apps]' not in text] == []


def test_new_app_copies_a_powershell_example(project, capsys):
    assert scaffold.new_app("my-report", "powershell-report") == 0
    assert (project / "my-report" / "report.ps1").is_file()
    assert not (project / "my-report" / "run.py").exists()
    assert find_apps() == ["my-report"]
    assert "my-report/report.ps1" in capsys.readouterr().out


def test_quick_start_copies_the_powershell_hello_world(project):
    # README's Quick start runs this exact `pdt new`, so the example must stay a
    # PowerShell app with one script the user can swap for their own.
    readme = (Path(__file__).resolve().parent.parent / "README.md").read_text()
    assert "pdt new hello-powershell --from powershell-hello-world" in readme
    assert scaffold.new_app("hello-powershell", "powershell-hello-world") == 0
    assert powershell_scripts(project / "hello-powershell") == ["hello.ps1"]
    assert find_apps() == ["hello-powershell"]
    assert validate_app("hello-powershell") == []


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


def test_replace_platform_keeps_every_other_line():
    text = "# mine\nplatform:\n  # which cloud\n  provider: azure\n  region: eastus2\n\napps: []\n"
    out = scaffold.replace_platform(text, {"provider": "aws", "region": "us-east-1"})
    assert out.startswith("# mine\n")
    assert yaml.safe_load(out) == {"platform": {"provider": "aws", "region": "us-east-1"},
                                   "apps": []}


@pytest.mark.parametrize("text", [
    "platform:\n  provider: azure\n# a comment at column zero\n  subscription: s\n\n# apps\napps: []\n",
    "#platform:\n#  provider: azure\nplatform:\n  provider: azure\n  subscription: s\napps: []\n",
    '"platform":\n  provider: azure\n  subscription: s\napps: []\n',
    "platform: {provider: azure, subscription: s}\napps: []\n",
])
def test_replace_platform_replaces_the_whole_active_block(text):
    out = scaffold.replace_platform(text, {"provider": "aws"})
    assert yaml.safe_load(out) == {"platform": {"provider": "aws"}, "apps": []}
    assert "subscription" not in out


def test_replace_platform_keeps_the_comment_above_the_next_key():
    out = scaffold.replace_platform("platform:\n  provider: azure\n\n# apps\napps: []\n",
                                    {"provider": "aws"})
    assert "# apps\napps: []" in out


def test_replace_platform_fills_in_the_commented_block():
    out = scaffold.replace_platform(scaffold.project_yaml({}), {"provider": "aws"})
    assert yaml.safe_load(out)["platform"] == {"provider": "aws"}
    assert "#  region" not in out


def test_replace_platform_adds_a_block_when_there_is_none():
    out = scaffold.replace_platform("apps: []\n", {"provider": "windows"})
    assert yaml.safe_load(out) == {"apps": [], "platform": {"provider": "windows"}}


@pytest.mark.parametrize("current,provider,kept", [
    ({"provider": "aws", "region": "us-east-1", "timezone": "America/Chicago"}, "aws",
     {"provider": "aws", "region": "us-east-1", "timezone": "America/Chicago"}),
    ({"provider": "aws", "account": "1", "timezone": "America/Chicago"}, "google-cloud",
     {"timezone": "America/Chicago"}),
    ({"provider": "aws", "timezone": "America/Chicago"}, "azure", {}),
    ({"provider": "aws", "timezone": "America/Chicago"}, "windows", {}),
    ({"provider": "windows", "timezone": "local"}, "aws", {}),
])
def test_a_move_keeps_only_the_settings_the_new_provider_reads(current, provider, kept):
    assert scaffold.kept_settings(current, provider) == kept


def answer(monkeypatch, replies, confirms=()):
    replies, confirms = iter(replies), iter(confirms)
    monkeypatch.setattr(scaffold.console, "ask", lambda question, default="": next(replies))
    monkeypatch.setattr(scaffold.console, "confirm", lambda *a, **k: next(confirms))


def deployed_app(project, name, config_text=""):
    add_app(project, name, config_text)
    from pdt import config
    config.mark_deployed(name, config.merged_app(name)["platform"])


class Cloud:
    """A provider that records each destroy and fails it for the apps in `failing`."""

    def __init__(self, monkeypatch, failing=()):
        self.destroyed = []
        self.failing = failing
        monkeypatch.setattr(scaffold.deploy, "dispatch", self.dispatch)

    def dispatch(self, provider, command, app_name, assume_yes, extra=None, rebuild=False,
                 platform=None):
        if app_name in self.failing:
            return 1
        self.destroyed.append((app_name, command, provider, assume_yes))
        return 0


def moving_project(project, names=("alpha", "beta", "gamma")):
    (project / PROJECT_FILE).write_text(
        "platform:\n  provider: azure\n  region: eastus2\n  subscription: sub-1\n")
    for name in names:
        deployed_app(project, name)
    return (project / PROJECT_FILE).read_text()


def platform_of(project, name):
    from pdt import config
    return config.load_yaml(project / name / APP_FILE).get("platform")


AZURE_PIN = {"provider": "azure", "region": "eastus2", "subscription": "sub-1"}


def test_move_all_destroys_each_app_then_saves_pdt_yml(project, monkeypatch, capsys):
    moving_project(project)
    cloud = Cloud(monkeypatch)
    answer(monkeypatch, ["2", "us-east-2", "UTC", "1"], [True])
    assert scaffold.init(None, assume_yes=False) == 0
    out = capsys.readouterr().out
    assert cloud.destroyed == [(name, "destroy", "azure", True) for name in ("alpha", "beta", "gamma")]
    assert yaml.safe_load((project / PROJECT_FILE).read_text())["platform"]["provider"] == "aws"
    assert not (project / "alpha" / APP_FILE).exists()
    assert "1  alpha  azure, region eastus2, subscription sub-1" in out
    assert "What should happen to the apps deployed on azure, region eastus2, subscription sub-1?" in out
    assert "3) Keep them on azure, region eastus2, subscription sub-1 (only new apps go to aws, region us-east-2)" in out
    for name in ("alpha", "beta", "gamma"):
        assert f"pdt deploy {name}" in out


def test_move_some_destroys_the_chosen_apps_and_pins_the_rest(project, monkeypatch, capsys):
    moving_project(project)
    cloud = Cloud(monkeypatch)
    answer(monkeypatch, ["2", "us-east-2", "UTC", "2", "2"], [True])
    assert scaffold.init(None, assume_yes=False) == 0
    out = capsys.readouterr().out
    assert [name for name, *_ in cloud.destroyed] == ["beta"]
    assert platform_of(project, "alpha") == AZURE_PIN
    assert platform_of(project, "gamma") == AZURE_PIN
    assert not (project / "beta" / APP_FILE).exists()
    assert "pdt deploy beta" in out
    assert "pdt deploy alpha" not in out


def test_keep_pins_every_deployed_app_and_destroys_nothing(project, monkeypatch, capsys):
    moving_project(project)
    cloud = Cloud(monkeypatch)
    answer(monkeypatch, ["2", "us-east-2", "UTC", "3"])
    assert scaffold.init(None, assume_yes=False) == 0
    assert cloud.destroyed == []
    from pdt import config
    for name in ("alpha", "beta", "gamma"):
        assert platform_of(project, name) == AZURE_PIN
        assert config.merged_app(name)["platform"]["provider"] == "azure"
    assert yaml.safe_load((project / PROJECT_FILE).read_text())["platform"]["provider"] == "aws"
    assert "alpha stays on azure, region eastus2, subscription sub-1." in capsys.readouterr().out


def test_move_all_destroys_a_disabled_app_that_is_deployed(project, monkeypatch):
    (project / PROJECT_FILE).write_text(
        "platform:\n  provider: azure\n  region: eastus2\n  subscription: sub-1\n")
    deployed_app(project, "alpha", "enabled: false\n")
    cloud = Cloud(monkeypatch)
    answer(monkeypatch, ["2", "us-east-2", "UTC", "1"], [True])
    assert scaffold.init(None, assume_yes=False) == 0
    assert [name for name, *_ in cloud.destroyed] == ["alpha"]


def test_keep_pins_the_region_an_environment_variable_selects(project, monkeypatch, capsys):
    monkeypatch.setenv("PDT_AZURE_REGION", "eastus2")
    (project / PROJECT_FILE).write_text("platform:\n  provider: azure\n  subscription: sub-1\n")
    deployed_app(project, "alpha")
    answer(monkeypatch, ["2", "us-east-2", "UTC", "3"])
    assert scaffold.init(None, assume_yes=False) == 0
    assert platform_of(project, "alpha") == AZURE_PIN
    assert "deployed on azure, region eastus2, subscription sub-1?" in capsys.readouterr().out


@pytest.mark.parametrize("replies,confirms", [
    (["4"], []),
    (["2", ""], []),
    (["1"], [False]),
])
def test_every_cancel_changes_nothing(project, monkeypatch, replies, confirms):
    before = moving_project(project)
    cloud = Cloud(monkeypatch)
    answer(monkeypatch, ["2", "us-east-2", "UTC", *replies], confirms)
    assert scaffold.init(None, assume_yes=False) == 0
    assert cloud.destroyed == []
    assert (project / PROJECT_FILE).read_text() == before
    assert not (project / "alpha" / APP_FILE).exists()


def test_a_failed_destroy_pins_each_app_still_deployed(project, monkeypatch, capsys):
    moving_project(project)
    cloud = Cloud(monkeypatch, failing=("beta",))
    answer(monkeypatch, ["2", "us-east-2", "UTC", "1"], [True])
    assert scaffold.init(None, assume_yes=False) == 1
    out = capsys.readouterr().out
    assert [name for name, *_ in cloud.destroyed] == ["alpha"]
    assert platform_of(project, "beta") == AZURE_PIN
    assert platform_of(project, "gamma") == AZURE_PIN
    assert yaml.safe_load((project / PROJECT_FILE).read_text())["platform"]["provider"] == "aws"
    assert "beta was not removed. It is still deployed on azure, region eastus2, subscription sub-1." in out
    assert "pdt deploy alpha" in out
    assert "pdt deploy gamma" not in out


def test_init_yes_in_a_project_moves_nothing(project, monkeypatch):
    before = moving_project(project)
    cloud = Cloud(monkeypatch)
    assert scaffold.init(None, assume_yes=True) == 0
    assert cloud.destroyed == []
    assert (project / PROJECT_FILE).read_text() == before


def test_init_again_moves_the_project_to_another_provider(project, monkeypatch):
    (project / PROJECT_FILE).write_text(
        "platform:\n  provider: aws\n  region: us-east-1\n  account: \"1\"\n"
        "  timezone: America/Chicago\n\nalerts: []\n")
    answer(monkeypatch, ["3", "us-east4", ""])
    assert scaffold.init(None, assume_yes=False) == 0
    assert yaml.safe_load((project / PROJECT_FILE).read_text()) == {
        "platform": {"provider": "google-cloud", "timezone": "America/Chicago",
                     "region": "us-east4"},
        "alerts": []}


def test_a_region_change_asks_too(project, monkeypatch, capsys):
    (project / PROJECT_FILE).write_text("platform:\n  provider: azure\n  region: eastus2\n")
    deployed_app(project, "report")
    answer(monkeypatch, ["1", "westeurope", "4"])
    assert scaffold.init(None, assume_yes=False) == 0
    assert "apps deployed on azure, region eastus2?" in capsys.readouterr().out


def test_an_app_with_its_own_provider_is_listed_without_a_number(project, monkeypatch, capsys):
    deployed_app(project, "own", "platform:\n  provider: google-cloud\n  region: us-east4\n")
    answer(monkeypatch, ["2", "us-east-2", ""])
    assert scaffold.init(None, assume_yes=False) == 0
    out = capsys.readouterr().out
    assert "\n  own  google-cloud, region us-east4; edit own/config.yml to move it\n" in out
    assert "What should happen" not in out
    assert platform_of(project, "own") == {"provider": "google-cloud", "region": "us-east4"}


def test_no_answer_at_the_move_question_changes_nothing(project, monkeypatch):
    deployed_app(project, "report")
    before = (project / PROJECT_FILE).read_text()
    replies = iter(["2", "us-east-2", ""])

    def ask(question, default=""):
        reply = next(replies, None)
        if reply is None:
            raise EOFError
        return reply

    monkeypatch.setattr(scaffold.console, "ask", ask)
    with pytest.raises(ConfigError, match="no one to answer"):
        scaffold.init(None, assume_yes=False)
    assert (project / PROJECT_FILE).read_text() == before


def test_init_again_reads_this_folder_when_pdt_project_names_another(project, monkeypatch,
                                                                       capsys, tmp_path_factory):
    other = tmp_path_factory.mktemp("other")
    (other / PROJECT_FILE).write_text("platform:\n  provider: azure\n")
    deployed_app(project, "report")
    monkeypatch.setenv("PDT_PROJECT", str(other))
    answer(monkeypatch, ["2", "us-east-2", "", "4"])
    assert scaffold.init(None, assume_yes=False) == 0
    assert "1  report  azure" in capsys.readouterr().out
    assert yaml.safe_load((other / PROJECT_FILE).read_text())["platform"]["provider"] == "azure"


@pytest.mark.parametrize("text", [
    "{platform: {provider: azure}, apps: []}\n",
    "platform: &defaults\n  provider: azure\napps:\n- name: report\n  platform: *defaults\n",
])
def test_replace_platform_refuses_a_form_it_cannot_change_safely(text):
    with pytest.raises(ConfigError, match="by hand"):
        scaffold.replace_platform(text, {"provider": "aws"})


def test_a_move_keeps_the_byte_order_mark_and_the_line_endings(project, monkeypatch):
    (project / PROJECT_FILE).write_bytes(
        "﻿# mine\r\nplatform:\r\n  provider: azure\r\n\r\napps: []\r\n".encode())
    answer(monkeypatch, ["2", "us-east-2", "UTC"])
    assert scaffold.init(None, assume_yes=False) == 0
    assert (project / PROJECT_FILE).read_bytes().decode() == (
        "﻿# mine\r\nplatform:\r\n  provider: \"aws\"\r\n  region: \"us-east-2\"\r\n"
        "  timezone: \"UTC\"\r\n\r\napps: []\r\n")


def test_ctrl_c_at_the_remove_question_changes_nothing(project, monkeypatch):
    deployed_app(project, "report")
    before = (project / PROJECT_FILE).read_text()
    answer(monkeypatch, ["2", "us-east-2", "", "1"])
    monkeypatch.setattr(scaffold.console, "confirm",
                        lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt))
    with pytest.raises(KeyboardInterrupt):
        scaffold.init(None, assume_yes=False)
    assert (project / PROJECT_FILE).read_text() == before


def test_init_again_puts_pdt_project_back(project, monkeypatch):
    answer(monkeypatch, ["5"])
    assert scaffold.init(None, assume_yes=False) == 0
    assert "PDT_PROJECT" not in os.environ


@pytest.mark.parametrize("text", [
    "defaults: &defaults\n  provider: azure\nplatform: *defaults\napps: []\n",
    "zone: &zone UTC\nplatform:\n  provider: aws\n  timezone: *zone\napps: []\n",
    "platform:\n  provider: azure\napps: []\n...\n",
])
def test_replace_platform_refuses_an_alias_or_a_document_end(text):
    with pytest.raises(ConfigError, match="by hand"):
        scaffold.replace_platform(text, {"provider": "aws"})


def test_replace_platform_leaves_out_an_empty_setting():
    out = scaffold.replace_platform("platform:\n  provider: azure\n  environment:\n",
                                    {"provider": "azure", "environment": None, "region": "eastus2"})
    assert yaml.safe_load(out) == {"platform": {"provider": "azure", "region": "eastus2"}}


def test_a_change_to_pdt_yml_during_the_questions_stops_the_move(project, monkeypatch):
    replies = iter(["2", "us-east-2"])

    def ask(question, default=""):
        reply = next(replies, None)
        if reply is None:
            (project / PROJECT_FILE).write_text("platform:\n  provider: google-cloud\n")
            return ""
        return reply

    monkeypatch.setattr(scaffold.console, "ask", ask)
    with pytest.raises(ConfigError, match="changed while pdt init was asking"):
        scaffold.init(None, assume_yes=False)
    assert (project / PROJECT_FILE).read_text() == "platform:\n  provider: google-cloud\n"
