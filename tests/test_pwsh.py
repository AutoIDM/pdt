import os
import subprocess
import sys
from pathlib import Path

import pytest

from pdt import pwsh


@pytest.mark.parametrize("system, machine, key", [
    ("Linux", "x86_64", "linux-x64"),
    ("Linux", "aarch64", "linux-arm64"),
    ("Darwin", "x86_64", "osx-x64"),
    ("Darwin", "arm64", "osx-arm64"),
    ("Windows", "AMD64", "win-x64"),
])
def test_platform_key_and_archive_name(monkeypatch, system, machine, key):
    monkeypatch.setattr(pwsh.platform, "system", lambda: system)
    monkeypatch.setattr(pwsh.platform, "machine", lambda: machine)
    assert pwsh.platform_key() == key
    assert key in pwsh.CHECKSUMS
    if key == "win-x64":
        assert pwsh.archive_name(key) == "PowerShell-7.6.6-win-x64.zip"
    else:
        assert pwsh.archive_name(key) == f"powershell-7.6.6-{key}.tar.gz"


def test_an_unknown_platform_names_the_install_page(monkeypatch):
    monkeypatch.setattr(pwsh.platform, "system", lambda: "Windows")
    monkeypatch.setattr(pwsh.platform, "machine", lambda: "ARM64")
    with pytest.raises(pwsh.PwshError, match="no pinned archive for Windows ARM64"):
        pwsh.platform_key()


def test_the_install_lives_in_the_machine_folder_on_windows_only(monkeypatch):
    monkeypatch.setenv("ProgramData", r"C:\ProgramData")
    monkeypatch.setenv("XDG_DATA_HOME", "/data")
    assert pwsh.install_dir(windows=True) == Path(r"C:\ProgramData") / "pdt" / "pwsh"
    assert pwsh.local_pwsh(windows=True) == Path(r"C:\ProgramData") / "pdt" / "pwsh" / "pwsh.exe"
    assert pwsh.install_dir(windows=False) == Path("/data/pdt/pwsh")
    assert pwsh.local_pwsh(windows=False) == Path("/data/pdt/pwsh/pwsh")


def test_pwsh_on_the_path_wins(monkeypatch):
    monkeypatch.setattr(pwsh.shutil, "which", lambda name: "/usr/bin/pwsh")
    assert pwsh.ensure_pwsh() == "/usr/bin/pwsh"


def test_a_missing_pwsh_downloads_without_a_question(monkeypatch, tmp_path):
    monkeypatch.setattr(pwsh.shutil, "which", lambda name: None)
    monkeypatch.setattr(pwsh, "local_pwsh", lambda windows=False: tmp_path / "pwsh")
    monkeypatch.setattr("builtins.input", lambda prompt="": pytest.fail(f"pdt asked: {prompt}"))
    downloads = []

    def fake_download(key):
        downloads.append(key)
        (tmp_path / "pwsh").write_text("")

    monkeypatch.setattr(pwsh, "download_pwsh", fake_download)
    assert pwsh.ensure_pwsh() == str(tmp_path / "pwsh")
    assert downloads == [pwsh.platform_key()]



def test_main_prints_only_the_pwsh_path_on_stdout(tmp_path):
    env = {**os.environ, "PATH": str(tmp_path), "XDG_DATA_HOME": str(tmp_path),
           "ProgramData": str(tmp_path), "HTTPS_PROXY": "http://127.0.0.1:9",
           "https_proxy": "http://127.0.0.1:9"}
    command = [sys.executable, "-m", "pdt.pwsh"]
    offline = subprocess.run(command, env=env, capture_output=True, text=True)
    assert offline.returncode == 1
    assert offline.stdout == ""
    assert "pwsh is not installed" in offline.stderr and "download failed" in offline.stderr

    installed = tmp_path / "pdt" / "pwsh" / ("pwsh.exe" if os.name == "nt" else "pwsh")
    installed.parent.mkdir(parents=True)
    installed.write_text("")
    found = subprocess.run(command, env=env, capture_output=True, text=True)
    assert found.returncode == 0
    assert found.stdout == f"{installed}\n"
