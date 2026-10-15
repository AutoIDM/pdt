import subprocess
from types import SimpleNamespace

import pytest

from conftest import add_app
from pdt import cli, config, upgrade

HEADER = '''#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["pdt-cli[apps]=={pin}", "requests"]
# ///
"""Says pdt-cli==0.0.1 in the docstring, which is not the pin."""
'''


@pytest.mark.parametrize("older, newer", [("0.1.9", "0.1.10"), ("0.1.9", "0.2.0"),
                                          ("0.0.0.dev0", "0.1.0"), ("1.0", "1.0.1")])
def test_release_compares_numbers_not_text(older, newer):
    assert upgrade.release(older) < upgrade.release(newer)


def test_an_older_pin_rises_to_the_newest_release():
    text, found = upgrade.raise_pin(HEADER.format(pin="0.1.8"), "0.2.0")
    assert found == "0.1.8"
    assert '"pdt-cli[apps]==0.2.0", "requests"' in text
    assert "pdt-cli==0.0.1 in the docstring" in text


@pytest.mark.parametrize("pin", ["0.2.0", "0.3.0"])
def test_a_pin_at_or_past_the_newest_release_stays(pin):
    text = HEADER.format(pin=pin)
    assert upgrade.raise_pin(text, "0.2.0") == (text, pin)


def test_a_run_py_with_no_script_header_has_no_pin():
    text = "import pdt\n# pdt-cli==0.1.0\n"
    assert upgrade.raise_pin(text, "0.2.0") == (text, None)


def test_raise_pins_writes_each_app_and_keeps_windows_line_endings(project, capsys):
    add_app(project, "old", run_body="")
    (project / "old" / "run.py").write_bytes(HEADER.format(pin="0.1.8").replace("\n", "\r\n")
                                             .encode())
    add_app(project, "current", run_body=HEADER.format(pin="0.2.0"))
    (project / "scripts").mkdir()
    (project / "scripts" / "main.ps1").write_text("Write-Output 'hi'\n")
    assert upgrade.raise_pins("0.2.0") == ["old"]
    raw = (project / "old" / "run.py").read_bytes()
    assert b'"pdt-cli[apps]==0.2.0"' in raw
    assert raw.count(b"\r\n") == HEADER.count("\n")
    out = capsys.readouterr().out
    assert "old  pdt-cli 0.1.8 to 0.2.0" in out
    assert "current  pdt-cli 0.2.0 (already the newest)" in out
    assert "scripts  runs the pdt that deploys it" in out


def test_upgrade_names_the_deploy_for_each_deployed_app_that_changed(project, monkeypatch,
                                                                     capsys):
    for name in ("shipped", "local"):
        add_app(project, name, run_body=HEADER.format(pin="0.1.8"))
    monkeypatch.setattr(upgrade, "newest", lambda: "0.2.0")
    monkeypatch.setattr(upgrade, "upgrade_tool", lambda current, latest: False)
    monkeypatch.setattr(config, "is_deployed", lambda name: name == "shipped")
    monkeypatch.setattr("sys.argv", ["pdt", "upgrade"])
    assert cli.main() == 0
    out = capsys.readouterr().out
    assert "Raised the pdt-cli pin in 2 app(s)." in out
    assert "pdt deploy shipped" in out
    assert "pdt deploy local" not in out


def test_upgrade_outside_a_project_upgrades_only_pdt(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("PDT_PROJECT", raising=False)
    monkeypatch.chdir(tmp_path)
    calls = []
    monkeypatch.setattr(upgrade, "newest", lambda: "0.2.0")
    monkeypatch.setattr(upgrade, "upgrade_tool", lambda current, latest: calls.append(latest))
    assert upgrade.main() == 0
    assert calls == ["0.2.0"]
    assert "Apps:" not in capsys.readouterr().out


def test_upgrade_fails_plainly_when_pypi_cannot_be_read(project, monkeypatch, capsys):
    def offline():
        raise upgrade.UpgradeError("could not read the newest pdt release")
    monkeypatch.setattr(upgrade, "newest", offline)
    assert upgrade.main() == 1
    assert "error: could not read the newest pdt release" in capsys.readouterr().out


def installed(monkeypatch, tmp_path, tools, local=""):
    """Make this pdt look like a uv tool under `tools`, not the clone the tests run from."""
    prefix = tools / "pdt-cli"
    package = prefix / "lib" / "site-packages" / "pdt"
    package.mkdir(parents=True)
    monkeypatch.setattr(upgrade, "__file__", str(package / "upgrade.py"))
    monkeypatch.setattr(upgrade.sys, "prefix", str(prefix))
    monkeypatch.setattr(upgrade, "uv_tool_dir", lambda: tools.resolve())
    monkeypatch.setenv("LOCALAPPDATA", local)
    return prefix


def test_a_uv_tool_install_is_a_uv_tool(tmp_path, monkeypatch):
    prefix = installed(monkeypatch, tmp_path, tmp_path / "uv" / "tools")
    assert upgrade.install_kind() == ("uv", prefix.resolve())


def test_the_winget_launchers_tool_folder_is_winget(tmp_path, monkeypatch):
    prefix = installed(monkeypatch, tmp_path, tmp_path / "AutoIDM" / "pdt" / "tools",
                       local=str(tmp_path))
    assert upgrade.install_kind() == ("winget", prefix.resolve())


def test_a_pdt_in_no_tool_folder_is_other(tmp_path, monkeypatch):
    installed(monkeypatch, tmp_path, tmp_path / "uv" / "tools")
    monkeypatch.setattr(upgrade.sys, "prefix", str(tmp_path / "venv"))
    assert upgrade.install_kind()[0] == "other"


def test_the_tests_run_from_a_clone():
    assert upgrade.install_kind()[0] == "clone"


def test_the_uv_upgrade_keeps_the_extras_and_allows_later_upgrades(tmp_path):
    (tmp_path / "uv-receipt.toml").write_text(
        '[tool]\nrequirements = [{ name = "pdt-cli", extras = ["apps"] }]\n')
    assert upgrade.upgrade_command("uv", tmp_path, "0.2.0") == [
        "uv", "tool", "install", "pdt-cli[apps]>=0.2.0"]
    assert upgrade.upgrade_command("uv", tmp_path / "missing", "0.2.0")[-1] == "pdt-cli>=0.2.0"


def test_winget_upgrades_its_own_package():
    command = upgrade.upgrade_command("winget", upgrade.Path("."), "0.2.0")
    assert command[:4] == ["winget", "upgrade", "--id", "AutoIDM.pdt"]


def test_a_uv_tool_upgrades_in_place_off_windows(tmp_path, monkeypatch, capsys):
    installed(monkeypatch, tmp_path, tmp_path / "uv" / "tools")
    monkeypatch.setattr(upgrade.sys, "platform", "linux")
    ran = []
    monkeypatch.setattr(upgrade.subprocess, "run",
                        lambda command: ran.append(command) or SimpleNamespace(returncode=0))
    assert upgrade.upgrade_tool("0.1.9", "0.2.0") is True
    assert ran == [["uv", "tool", "install", "pdt-cli>=0.2.0"]]
    assert "pdt is now 0.2.0." in capsys.readouterr().out


def test_a_failed_uv_upgrade_is_an_error(tmp_path, monkeypatch):
    installed(monkeypatch, tmp_path, tmp_path / "uv" / "tools")
    monkeypatch.setattr(upgrade.sys, "platform", "linux")
    monkeypatch.setattr(upgrade.subprocess, "run", lambda command: SimpleNamespace(returncode=2))
    with pytest.raises(upgrade.UpgradeError, match="failed"):
        upgrade.upgrade_tool("0.1.9", "0.2.0")


def test_windows_upgrades_after_pdt_ends(tmp_path, monkeypatch, capsys):
    installed(monkeypatch, tmp_path, tmp_path / "AutoIDM" / "pdt" / "tools",
              local=str(tmp_path))
    monkeypatch.setattr(upgrade.sys, "platform", "win32")
    later = []
    monkeypatch.setattr(upgrade, "after_exit", later.append)
    assert upgrade.upgrade_tool("0.1.9", "0.2.0") is True
    assert later[0][:2] == ["winget", "upgrade"]
    assert "in a new window, once this command ends" in capsys.readouterr().out


def test_the_newest_pdt_is_left_alone(tmp_path, monkeypatch, capsys):
    installed(monkeypatch, tmp_path, tmp_path / "uv" / "tools")
    monkeypatch.setattr(upgrade.subprocess, "run", None)
    assert upgrade.upgrade_tool("0.2.0", "0.2.0") is False
    assert "pdt 0.2.0 is the newest release." in capsys.readouterr().out


def test_the_window_waits_for_each_pdt_process_then_runs_the_command(monkeypatch):
    monkeypatch.setattr(upgrade.shutil, "which", lambda name: "powershell")
    monkeypatch.setattr(upgrade.subprocess, "run",
                        lambda *args, **kwargs: SimpleNamespace(stdout="41\r\n7\r\n"))
    started = []
    monkeypatch.setattr(upgrade.subprocess, "Popen",
                        lambda command, creationflags: started.append(command))
    upgrade.after_exit(["uv", "tool", "install", "pdt-cli>=0.2.0"])
    script = started[0][-1]
    assert "foreach ($id in @(41,7))" in script
    assert "& 'uv' 'tool' 'install' 'pdt-cli>=0.2.0'" in script


@pytest.mark.parametrize("branch, changes, pulled", [("main", "", True),
                                                     ("feature", "", False),
                                                     ("main", "M src/pdt/cli.py", False)])
def test_a_clone_pulls_only_on_a_clean_main(tmp_path, monkeypatch, branch, changes, pulled):
    answers = {"rev-parse": branch, "status": changes}
    monkeypatch.setattr(upgrade, "git", lambda root, command, *rest: answers[command])
    ran = []
    monkeypatch.setattr(upgrade.subprocess, "run",
                        lambda command: ran.append(command) or SimpleNamespace(returncode=0))
    assert upgrade.pull_clone(tmp_path) is pulled
    assert ran == ([["git", "-C", str(tmp_path), "pull", "--ff-only"]] if pulled else [])


def test_a_clone_that_git_cannot_read_is_left_alone(tmp_path, monkeypatch):
    def broken(*args):
        raise subprocess.CalledProcessError(128, "git")
    monkeypatch.setattr(upgrade, "git", broken)
    assert upgrade.pull_clone(tmp_path) is False
