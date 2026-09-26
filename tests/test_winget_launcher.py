import importlib.util
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "winget_launcher.py"
spec = importlib.util.spec_from_file_location("winget_launcher", SCRIPT)
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)

HOME = Path("C:/Users/me/AppData/Local/AutoIDM/pdt")
INSTALL = ["uv", "tool", "install", f"pdt-cli=={launcher.VERSION}"]


class FakeRun:
    def __init__(self, install_code=0, pdt_code=0):
        self.commands = []
        self.install_env = None
        self.install_code = install_code
        self.pdt_code = pdt_code

    def __call__(self, command, **kwargs):
        self.commands.append(command)
        if command == INSTALL:
            self.install_env = kwargs["env"]
            return subprocess.CompletedProcess(command, self.install_code, "", "no such version\n")
        return subprocess.CompletedProcess(command, self.pdt_code)


@pytest.fixture
def uv_on_path(monkeypatch):
    monkeypatch.setattr(launcher.shutil, "which", lambda name: "uv")
    monkeypatch.setenv("LOCALAPPDATA", "C:/Users/me/AppData/Local")


def test_missing_uv_prints_the_winget_hint(monkeypatch, capsys):
    monkeypatch.setattr(launcher.shutil, "which", lambda name: None)
    assert launcher.main(["list"]) == 1
    assert "winget install astral-sh.uv" in capsys.readouterr().err


def test_installs_the_pinned_version_then_runs_pdt_with_the_same_arguments(monkeypatch, uv_on_path):
    run = FakeRun(pdt_code=3)
    monkeypatch.setattr(launcher.subprocess, "run", run)
    assert launcher.main(["deploy", "my-report", "--yes"]) == 3
    assert run.commands == [
        INSTALL,
        [str(HOME / "bin" / "pdt.exe"), "deploy", "my-report", "--yes"],
    ]


def test_installs_into_its_own_folders_off_the_path(monkeypatch, uv_on_path):
    run = FakeRun()
    monkeypatch.setattr(launcher.subprocess, "run", run)
    launcher.main(["list"])
    assert run.install_env["UV_TOOL_DIR"] == str(HOME / "tools")
    assert run.install_env["UV_TOOL_BIN_DIR"] == str(HOME / "bin")


def test_failed_install_shows_uv_error_and_exits_1(monkeypatch, uv_on_path, capsys):
    run = FakeRun(install_code=2)
    monkeypatch.setattr(launcher.subprocess, "run", run)
    assert launcher.main(["list"]) == 1
    assert "no such version" in capsys.readouterr().err
    assert run.commands == [INSTALL]
