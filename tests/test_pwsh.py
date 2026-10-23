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
    ("Windows", "ARM64", "win-arm64"),
])
def test_platform_key_and_archive_name(monkeypatch, system, machine, key):
    monkeypatch.setattr(pwsh.platform, "system", lambda: system)
    monkeypatch.setattr(pwsh.platform, "machine", lambda: machine)
    assert pwsh.platform_key() == key
    assert key in pwsh.CHECKSUMS
    if key.startswith("win-"):
        assert pwsh.archive_name(key) == f"PowerShell-7.6.8-{key}.zip"
    else:
        assert pwsh.archive_name(key) == f"powershell-7.6.8-{key}.tar.gz"


def test_an_unknown_platform_names_the_install_page(monkeypatch):
    monkeypatch.setattr(pwsh.platform, "system", lambda: "FreeBSD")
    monkeypatch.setattr(pwsh.platform, "machine", lambda: "amd64")
    with pytest.raises(pwsh.PwshError, match="no pinned archive for FreeBSD amd64"):
        pwsh.platform_key()


def test_the_install_lives_in_the_machine_folder_on_windows_only(monkeypatch):
    monkeypatch.setenv("ProgramData", r"C:\ProgramData")
    monkeypatch.setattr(pwsh, "data_home", lambda: Path("/data"))
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


def local_copy(monkeypatch, tmp_path, version):
    monkeypatch.setattr(pwsh.shutil, "which", lambda name: None)
    monkeypatch.setattr(pwsh, "install_dir", lambda windows=False: tmp_path / "pwsh")
    (tmp_path / "pwsh").mkdir()
    pwsh.local_pwsh().write_text("")
    if version:
        (tmp_path / "pwsh" / "VERSION").write_text(version)


@pytest.mark.parametrize("version, line", [("7.6.6", f"Updating PowerShell 7.6.6 to {pwsh.VERSION}"),
                                           (None, f"Updating PowerShell to {pwsh.VERSION}")])
def test_a_local_pwsh_of_another_version_is_replaced(monkeypatch, tmp_path, capsys, version, line):
    local_copy(monkeypatch, tmp_path, version)
    downloads = []
    monkeypatch.setattr(pwsh, "download_pwsh", downloads.append)
    assert pwsh.ensure_pwsh() == str(pwsh.local_pwsh())
    assert downloads == [pwsh.platform_key()]
    assert line in capsys.readouterr().out


def test_a_local_pwsh_of_the_pinned_version_is_kept(monkeypatch, tmp_path):
    local_copy(monkeypatch, tmp_path, pwsh.VERSION)
    monkeypatch.setattr(pwsh, "download_pwsh", lambda key: pytest.fail("downloaded"))
    assert pwsh.ensure_pwsh() == str(pwsh.local_pwsh())


def test_pwsh_on_the_path_wins_over_a_local_pwsh_of_another_version(monkeypatch, tmp_path):
    local_copy(monkeypatch, tmp_path, "7.6.6")
    monkeypatch.setattr(pwsh.shutil, "which", lambda name: "/usr/bin/pwsh")
    monkeypatch.setattr(pwsh, "download_pwsh", lambda key: pytest.fail("downloaded"))
    assert pwsh.ensure_pwsh() == "/usr/bin/pwsh"
    assert (tmp_path / "pwsh" / "VERSION").read_text() == "7.6.6"


def test_the_download_replaces_the_local_pwsh_only_after_it_unpacks(monkeypatch, tmp_path):
    local_copy(monkeypatch, tmp_path, "7.6.6")

    def refuse(url, sha256, stage, error):
        raise error("checksum mismatch")

    monkeypatch.setattr(pwsh, "fetch_verified", refuse)
    with pytest.raises(pwsh.PwshError, match="checksum mismatch"):
        pwsh.download_pwsh("osx-arm64")
    assert (tmp_path / "pwsh" / "VERSION").read_text() == "7.6.6"

    def unpack(url, sha256, stage, error):
        stage.mkdir()
        (stage / "pwsh").write_text(pwsh.VERSION)

    monkeypatch.setattr(pwsh, "fetch_verified", unpack)
    pwsh.download_pwsh("osx-arm64")
    assert (tmp_path / "pwsh" / "VERSION").read_text() == pwsh.VERSION
    assert (tmp_path / "pwsh" / "pwsh").read_text() == pwsh.VERSION
    assert sorted(path.name for path in tmp_path.iterdir()) == ["pwsh"]


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
    (installed.parent / "VERSION").write_text(pwsh.VERSION)
    found = subprocess.run(command, env=env, capture_output=True, text=True)
    assert found.returncode == 0
    assert found.stdout == f"{installed}\n"
