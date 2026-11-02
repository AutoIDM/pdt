import io
import json
from types import SimpleNamespace

import pytest

from conftest import add_app
from pdt import cli, config, upgrade

HEADER = '''#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["pdt-cli[apps]=={pin}", "requests"]
# ///
"""Says "pdt-cli==0.0.1" in the docstring, which is not the pin."""
'''


@pytest.fixture
def pypi(monkeypatch):
    """PyPI names 0.2.0 as the newest release, and this pdt is 0.1.9 run from a clone."""
    body = json.dumps({"info": {"version": "0.2.0"}}).encode()
    monkeypatch.setattr(upgrade.urllib.request, "urlopen", lambda url, timeout: io.BytesIO(body))
    monkeypatch.setattr("pdt.__version__", "0.1.9", raising=False)
    monkeypatch.setattr("sys.argv", ["pdt", "upgrade"])


def test_upgrade_raises_each_pin_and_names_the_deploys(project, pypi, monkeypatch, capsys):
    add_app(project, "shipped", run_body=HEADER.format(pin="0.1.8"))
    add_app(project, "local", run_body=HEADER.format(pin="0.1.8"))
    add_app(project, "current", run_body=HEADER.format(pin="0.2.0"))
    monkeypatch.setattr(config, "is_deployed", lambda name: name == "shipped")
    assert cli.main() == 0
    text = (project / "shipped" / "run.py").read_text()
    assert '"pdt-cli[apps]==0.2.0", "requests"' in text
    assert '"pdt-cli==0.0.1" in the docstring' in text
    out = capsys.readouterr().out
    assert "shipped: pdt-cli 0.1.8 to 0.2.0" in out
    assert "current:" not in out
    assert "pdt deploy shipped" in out
    assert "pdt deploy local" not in out


def test_a_raised_pin_keeps_windows_line_endings(project, pypi):
    folder = add_app(project, "report")
    (folder / "run.py").write_bytes(HEADER.format(pin="0.1.8").replace("\n", "\r\n").encode())
    assert upgrade.raise_pins("0.2.0") == (["report"], False)
    raw = (folder / "run.py").read_bytes()
    assert b'"pdt-cli[apps]==0.2.0"' in raw
    assert raw.count(b"\r\n") == HEADER.count("\n")


def test_upgrade_outside_a_project_upgrades_only_pdt(tmp_path, pypi, monkeypatch):
    monkeypatch.delenv("PDT_PROJECT", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(upgrade.config, "app_folders", None)
    assert upgrade.main() == 0


def test_upgrade_fails_plainly_when_pypi_cannot_be_read(project, monkeypatch, capsys):
    def offline(url, timeout):
        raise OSError("no network")
    monkeypatch.setattr(upgrade.urllib.request, "urlopen", offline)
    assert upgrade.main() == 1
    assert "could not read the newest pdt release" in capsys.readouterr().out


@pytest.fixture
def uv_tool(tmp_path_factory, monkeypatch):
    """This pdt is a uv tool in its own folders, as the MSI and the winget zip install it."""
    home = tmp_path_factory.mktemp("home")
    prefix = home / "tools" / "pdt-cli"
    prefix.mkdir(parents=True)
    entry = home / "bin" / "pdt"
    (prefix / "uv-receipt.toml").write_text(
        f'[tool]\nentrypoints = [{{ name = "pdt", install-path = "{entry}", from = "pdt-cli" }}]\n')
    monkeypatch.setattr(upgrade.sys, "prefix", str(prefix))
    return home


def test_a_uv_tool_upgrades_in_its_own_folders_then_raises_the_pins(project, pypi, uv_tool,
                                                                      monkeypatch):
    add_app(project, "report", run_body=HEADER.format(pin="0.1.8"))
    monkeypatch.setattr(upgrade.sys, "platform", "linux")
    ran = []

    def run(command, env):
        assert '==0.1.8"' in (project / "report" / "run.py").read_text()
        ran.append((command, env["UV_TOOL_DIR"], env["UV_TOOL_BIN_DIR"]))
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(upgrade.subprocess, "run", run)
    assert upgrade.main() == 0
    assert ran == [(["uv", "tool", "install", "pdt-cli>=0.2.0"],
                    str(uv_tool / "tools"), str(uv_tool / "bin"))]
    assert '==0.2.0"' in (project / "report" / "run.py").read_text()


def test_a_failed_install_leaves_the_pins(project, pypi, uv_tool, monkeypatch, capsys):
    add_app(project, "report", run_body=HEADER.format(pin="0.1.8"))
    monkeypatch.setattr(upgrade.sys, "platform", "linux")
    monkeypatch.setattr(upgrade.subprocess, "run",
                        lambda command, env: SimpleNamespace(returncode=2))
    assert upgrade.main() == 2
    assert '==0.1.8"' in (project / "report" / "run.py").read_text()
    assert "uv exited with code 2, so pdt and the app pins stay as they are." in (
        capsys.readouterr().out)


def test_windows_upgrades_in_a_new_window_that_then_raises_the_pins(project, pypi, uv_tool,
                                                                    monkeypatch, capsys):
    add_app(project, "report", run_body=HEADER.format(pin="0.1.8"))
    monkeypatch.setattr(upgrade.sys, "platform", "win32")
    monkeypatch.setattr(upgrade.subprocess, "CREATE_NEW_CONSOLE", 16, raising=False)
    monkeypatch.setattr(upgrade.os, "getpid", lambda: 41)
    started = []
    monkeypatch.setattr(upgrade.subprocess, "Popen",
                        lambda command, env, creationflags: started.append((command[-1], env)))
    assert upgrade.main() == 0
    script, env = started[0]
    entry = str(uv_tool / "bin" / "pdt")
    assert "Wait-Process -Id 41" in script
    assert f"$_.Path -eq '{entry}'" in script
    assert f"StartsWith('{uv_tool / 'tools' / 'pdt-cli'}\\', 'OrdinalIgnoreCase')" in script
    assert "uv tool install 'pdt-cli>=0.2.0'; if ($LASTEXITCODE)" in script
    assert f"else {{ & '{entry}' upgrade }}" in script
    assert env["UV_TOOL_DIR"] == str(uv_tool / "tools")
    assert '==0.1.8"' in (project / "report" / "run.py").read_text()
    assert "That window then raises the app pins." in capsys.readouterr().out


def test_the_newest_pdt_is_left_alone(project, pypi, uv_tool, monkeypatch, capsys):
    monkeypatch.setattr("pdt.__version__", "0.2.0")
    monkeypatch.setattr(upgrade.subprocess, "run", None)
    assert upgrade.main() == 0
    assert "pdt 0.2.0 is the newest release." in capsys.readouterr().out


def test_a_pdt_that_is_not_a_uv_tool_is_left_alone(project, pypi, tmp_path_factory, monkeypatch,
                                                   capsys):
    monkeypatch.setattr(upgrade.sys, "prefix", str(tmp_path_factory.mktemp("venv")))
    monkeypatch.setattr(upgrade.subprocess, "run", None)
    assert upgrade.main() == 0
    assert "which is not a uv tool" in capsys.readouterr().out


def test_a_run_py_that_fails_is_named_and_the_other_apps_move(project, pypi, monkeypatch, capsys):
    (add_app(project, "latin1") / "run.py").write_bytes(b"# caf\xe9\n")
    locked = add_app(project, "locked", run_body=HEADER.format(pin="0.1.8"))
    add_app(project, "shipped", run_body=HEADER.format(pin="0.1.8"))
    monkeypatch.setattr(config, "is_deployed", lambda name: name == "shipped")
    real_open = open

    def open_(path, mode="r", **kwargs):
        if path == locked / "run.py" and mode == "w":
            raise PermissionError(13, "Permission denied")
        return real_open(path, mode, **kwargs)
    monkeypatch.setattr("builtins.open", open_)
    assert upgrade.main() == 1
    out = capsys.readouterr().out
    assert f"could not raise the pdt-cli pin in {project / 'latin1' / 'run.py'}" in out
    assert f"could not raise the pdt-cli pin in {locked / 'run.py'}" in out
    assert "Traceback" not in out
    assert "shipped: pdt-cli 0.1.8 to 0.2.0" in out
    assert "pdt deploy shipped" in out
