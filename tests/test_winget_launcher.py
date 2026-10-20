import importlib.util
import subprocess
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "winget_launcher.py"
spec = importlib.util.spec_from_file_location("winget_launcher", SCRIPT)
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)

HOME = Path("C:/Users/me/AppData/Local/AutoIDM/pdt")
INSTALL = ["uv", "tool", "install", "--python", "3.12", "--compile-bytecode", f"pdt-cli=={launcher.VERSION}"]


UV_LINES = [
    b"Downloading cpython-3.14.7-windows-x86_64-none (download) (21.5MiB)\n",
    b" Downloaded cpython-3.14.7-windows-x86_64-none (download)\n",
    b"Resolved 27 packages in 950ms\n",
    b"Downloading cryptography (3.7MiB)\n",
    b" Downloaded cryptography\n",
    b"Prepared 27 packages in 702ms\n",
    b"Installed 27 packages in 23ms\n",
    b" + pdt-cli==0.1.10\n",
    b"Installed 1 executable: pdt\n",
    b"warning: `C:\\Users\\me\\AppData\\Local\\AutoIDM\\pdt\\bin` is not on your PATH."
    b" To use installed tools, run `uv tool update-shell`.\n",
]
UV_FAILURE = [
    "  \u00d7 No solution found when resolving dependencies:\n".encode(),
    "  \u2570\u2500\u25b6 Because there is no version of pdt-cli==0.0.0 and you require\n".encode(),
]


class FakeUv:
    def __init__(self, install_code=0, pdt_code=0, lines=UV_LINES, pause=0.0):
        self.commands = []
        self.install_env = None
        self.install_code = install_code
        self.pdt_code = pdt_code
        self.lines = lines
        self.pause = pause

    def popen(self, command, **kwargs):
        self.commands.append(command)
        self.install_env = kwargs["env"]
        return FakeProcess(self.stdout(), self.install_code)

    def stdout(self):
        time.sleep(self.pause)
        yield from self.lines

    def run(self, command, **kwargs):
        self.commands.append(command)
        return subprocess.CompletedProcess(command, self.pdt_code)


class FakeProcess:
    def __init__(self, stdout, code):
        self.stdout = stdout
        self.code = code

    def wait(self):
        return self.code


@pytest.fixture(autouse=True)
def launcher_folder(monkeypatch, tmp_path):
    monkeypatch.setattr(launcher.sys, "executable", str(tmp_path / "pdt.exe"))
    return tmp_path


@pytest.fixture
def uv_on_path(monkeypatch):
    monkeypatch.setattr(launcher.shutil, "which", lambda name: "uv")
    monkeypatch.setenv("LOCALAPPDATA", "C:/Users/me/AppData/Local")


def fake_uv(monkeypatch, **kwargs):
    uv = FakeUv(**kwargs)
    monkeypatch.setattr(launcher.subprocess, "Popen", uv.popen)
    monkeypatch.setattr(launcher.subprocess, "run", uv.run)
    return uv


def test_uv_beside_the_launcher_comes_before_the_path(monkeypatch, uv_on_path, launcher_folder):
    bundled = launcher_folder / "uv.exe"
    bundled.write_bytes(b"")
    run = fake_uv(monkeypatch)
    assert launcher.main(["list"]) == 0
    assert run.commands[0] == [str(bundled), *INSTALL[1:]]


def test_without_uv_beside_the_launcher_it_uses_the_path(monkeypatch, uv_on_path):
    run = fake_uv(monkeypatch)
    launcher.main(["list"])
    assert run.commands[0] == INSTALL


def test_missing_uv_prints_the_winget_hint(monkeypatch, capsys):
    monkeypatch.setattr(launcher.shutil, "which", lambda name: None)
    assert launcher.main(["list"]) == 1
    err = capsys.readouterr().err
    assert "winget install astral-sh.uv" in err
    assert "open a new PowerShell window" in err


def test_installs_the_pinned_version_then_runs_pdt_with_the_same_arguments(monkeypatch, uv_on_path):
    run = fake_uv(monkeypatch, pdt_code=3)
    assert launcher.main(["deploy", "my-report", "--yes"]) == 3
    assert run.commands == [
        INSTALL,
        [str(HOME / "bin" / "pdt.exe"), "deploy", "my-report", "--yes"],
    ]


def test_installs_into_its_own_folders_off_the_path(monkeypatch, uv_on_path):
    run = fake_uv(monkeypatch)
    launcher.main(["list"])
    assert run.install_env["UV_TOOL_DIR"] == str(HOME / "tools")
    assert run.install_env["UV_TOOL_BIN_DIR"] == str(HOME / "bin")


def test_failed_install_exits_1_without_running_pdt(monkeypatch, uv_on_path):
    run = fake_uv(monkeypatch, install_code=2)
    assert launcher.main(["list"]) == 1
    assert run.commands == [INSTALL]


def test_terminal_shows_one_status_line_that_ends_done(monkeypatch, uv_on_path, capsys):
    fake_uv(monkeypatch, pause=0.05)
    monkeypatch.setattr(launcher.sys.stderr, "isatty", lambda: True)
    launcher.main(["list"])
    err = capsys.readouterr().err
    assert err.startswith("\r")
    assert err.count("\n") == 1
    assert err.split("\r")[-1].rstrip() == "Setting up pdt: done (0s)"


def test_status_line_counts_seconds_with_no_phase_names(monkeypatch, uv_on_path, capsys):
    fake_uv(monkeypatch, pause=0.05)
    monkeypatch.setattr(launcher.sys.stderr, "isatty", lambda: True)
    monkeypatch.setattr(launcher, "TICK", 1.0)
    launcher.main(["list"])
    first, done = capsys.readouterr().err.split("\r")[1:]
    assert first == "Setting up pdt (0s)"
    assert done == "Setting up pdt: done (0s)\n"


def test_not_a_terminal_prints_the_start_and_done_lines_only(monkeypatch, uv_on_path, capsys):
    fake_uv(monkeypatch)
    launcher.main(["list"])
    assert capsys.readouterr().err == (
        "Setting up pdt: started\n"
        "Setting up pdt: done (0s)\n"
    )


def test_uv_package_list_and_path_warning_never_print(monkeypatch, uv_on_path, capsys):
    fake_uv(monkeypatch, pause=0.05)
    monkeypatch.setattr(launcher.sys.stderr, "isatty", lambda: True)
    launcher.main(["list"])
    err = capsys.readouterr().err
    assert "PATH" not in err
    assert "update-shell" not in err
    assert "pdt-cli==" not in err


def test_failed_install_prints_what_uv_said(monkeypatch, uv_on_path, capsys):
    fake_uv(monkeypatch, install_code=1, lines=UV_FAILURE)
    assert launcher.main(["list"]) == 1
    assert capsys.readouterr().err == (
        "Setting up pdt: started\n"
        "Setting up pdt: failed (0s)\n"
        + b"".join(UV_FAILURE).decode()
    )


def test_installed_version_runs_pdt_without_uv(monkeypatch, uv_on_path, tmp_path, capsys):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    home = tmp_path / "AutoIDM" / "pdt"
    (home / "tools" / "pdt-cli" / "Lib" / "site-packages" / f"pdt_cli-{launcher.VERSION}.dist-info").mkdir(parents=True)
    run = fake_uv(monkeypatch)
    assert launcher.main(["list"]) == 0
    assert run.commands == [[str(home / "bin" / "pdt.exe"), "list"]]
    assert capsys.readouterr().err == ""
