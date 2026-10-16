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
    assert upgrade.raise_pins("0.2.0") == ["report"]
    raw = (folder / "run.py").read_bytes()
    assert b'"pdt-cli[apps]==0.2.0"' in raw
    assert raw.count(b"\r\n") == HEADER.count("\n")


def test_upgrade_outside_a_project_upgrades_only_pdt(tmp_path, pypi, monkeypatch):
    monkeypatch.delenv("PDT_PROJECT", raising=False)
    monkeypatch.chdir(tmp_path)
    calls = []
    monkeypatch.setattr(upgrade, "upgrade_pdt", lambda current, latest: calls.append(latest) or 0)
    assert upgrade.main() == 0
    assert calls == ["0.2.0"]


def test_upgrade_fails_plainly_when_pypi_cannot_be_read(project, monkeypatch, capsys):
    def offline(url, timeout):
        raise OSError("no network")
    monkeypatch.setattr(upgrade.urllib.request, "urlopen", offline)
    assert upgrade.main() == 1
    assert "could not read the newest pdt release" in capsys.readouterr().out


@pytest.fixture
def uv_tool(tmp_path, monkeypatch):
    (tmp_path / "uv-receipt.toml").write_text("[tool]\n")
    monkeypatch.setattr(upgrade.sys, "prefix", str(tmp_path))


def test_a_uv_tool_upgrades_in_place(uv_tool, monkeypatch):
    monkeypatch.setattr(upgrade.sys, "platform", "linux")
    ran = []
    monkeypatch.setattr(upgrade.subprocess, "run",
                        lambda command: ran.append(command) or SimpleNamespace(returncode=0))
    assert upgrade.upgrade_pdt("0.1.9", "0.2.0") == 0
    assert ran == [["uv", "tool", "install", "pdt-cli>=0.2.0"]]


def test_windows_upgrades_in_a_new_window_after_pdt_ends(uv_tool, monkeypatch, capsys):
    monkeypatch.setattr(upgrade.sys, "platform", "win32")
    monkeypatch.setattr(upgrade.subprocess, "CREATE_NEW_CONSOLE", 16, raising=False)
    monkeypatch.setattr(upgrade.os, "getpid", lambda: 41)
    monkeypatch.setattr(upgrade.os, "getppid", lambda: 7)
    started = []
    monkeypatch.setattr(upgrade.subprocess, "Popen",
                        lambda command, creationflags: started.append(command))
    assert upgrade.upgrade_pdt("0.1.9", "0.2.0") == 0
    assert "Wait-Process -Id 41,7" in started[0][-1]
    assert "uv tool install 'pdt-cli>=0.2.0'" in started[0][-1]
    assert "in a new window once this command ends" in capsys.readouterr().out


def test_the_newest_pdt_is_left_alone(uv_tool, monkeypatch, capsys):
    monkeypatch.setattr(upgrade.subprocess, "run", None)
    assert upgrade.upgrade_pdt("0.2.0", "0.2.0") == 0
    assert "pdt 0.2.0 is the newest release." in capsys.readouterr().out


def test_a_pdt_that_is_not_a_uv_tool_is_left_alone(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(upgrade.sys, "prefix", str(tmp_path))
    monkeypatch.setattr(upgrade.subprocess, "run", None)
    assert upgrade.upgrade_pdt("0.1.9", "0.2.0") == 0
    assert "which is not a uv tool" in capsys.readouterr().out
