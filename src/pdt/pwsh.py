"""Find pwsh, or install a pinned PowerShell 7 in the pdt data folder.

Search order: pwsh on PATH, then the local install. If neither exists,
download the pinned version below (~70 MB, ~110 MB on Windows) from
github.com, verify its sha256, and unpack it, with no question, the same
way gcloud_sdk.py installs the Google Cloud CLI. On macOS and Linux the
install lives in the user's data folder. On Windows it lives in
%ProgramData%\\pdt\\pwsh, because the scheduled task runs as SYSTEM and
must reach the same pwsh the deploying user installed. A local install
of another version (the VERSION file pdt writes into it) is replaced the
same way, and the current one stays until the new one is unpacked.

To bump the pin, take the new version number and the six checksums from
the `hashes.sha256` asset of the release at
https://github.com/PowerShell/PowerShell/releases
"""

from __future__ import annotations

import contextlib
import os
import platform
import shutil
import sys
from pathlib import Path

from pdt import console
from pdt.config import data_home, locked, machine_data_home, unsaved
from pdt.download import fetch_verified

VERSION = "7.6.10"
CHECKSUMS = {
    "linux-x64": "e261cabc91c78a9afe8d1e5f24731ab0d38204735ab2ec94b0af55102652e10d",
    "linux-arm64": "439432cdecf4418413b9a8f547e3ab4d9b40e8e4258913ec3d15fc92c67a9235",
    "osx-x64": "aa55bef6375e7b1a61e860866a38af5f2366ceb4189293e6b73b7777cb1d7793",
    "osx-arm64": "fb6f1978c7d3266d4ced9391de6519d971ef92bd036e5b8be62f1ccdfbda9477",
    "win-x64": "7d871531806eb1646264e4bbabd9f0c30c23355591bc756bb9909448c29ae166",
    "win-arm64": "1d36ecac1d8f5ac9577cfdfc426af41f3e90cbca6c52445e7db167f532b3a7ab",
}
INSTALL_DOCS = "https://learn.microsoft.com/powershell/scripting/install/installing-powershell"


class PwshError(Exception):
    pass


def platform_key() -> str:
    system = platform.system().lower()
    machine = platform.machine().lower()
    arm = machine in ("arm64", "aarch64")
    if system == "linux":
        return "linux-arm64" if arm else "linux-x64"
    if system == "darwin":
        return "osx-arm64" if arm else "osx-x64"
    if system == "windows":
        if arm:
            return "win-arm64"
        if machine in ("amd64", "x86_64"):
            return "win-x64"
    raise PwshError(
        f"no pinned archive for {platform.system()} {platform.machine()}; "
        f"install PowerShell 7 from {INSTALL_DOCS}")


def archive_name(key: str) -> str:
    # Microsoft capitalises the Windows zip and not the tar.gz files.
    if key.startswith("win-"):
        return f"PowerShell-{VERSION}-{key}.zip"
    return f"powershell-{VERSION}-{key}.tar.gz"


def install_dir(windows: bool = os.name == "nt") -> Path:
    if windows:
        return machine_data_home() / "pwsh"
    return data_home() / "pdt" / "pwsh"


def local_pwsh(windows: bool = os.name == "nt") -> Path:
    return install_dir(windows) / ("pwsh.exe" if windows else "pwsh")


def ensure_pwsh() -> str:
    found = shutil.which("pwsh")
    if found:
        return found
    previous = install_dir().with_name(f"{install_dir().name}.previous")
    if unsaved() is not None:
        for folder in (install_dir(), previous):
            if (folder / local_pwsh().name).is_file():
                return str(folder / local_pwsh().name)
        raise PwshError("pwsh is not installed, and a plan installs nothing; run the deploy "
                        "without --plan to install it")
    version_file = install_dir() / "VERSION"
    # Another process can move the install away between the check and the read.
    with contextlib.suppress(FileNotFoundError):
        if local_pwsh().is_file() and version_file.read_text().strip() == VERSION:
            return str(local_pwsh())
    install_dir().parent.mkdir(parents=True, exist_ok=True)
    with locked(install_dir()):
        if previous.is_dir() and not install_dir().exists():
            previous.rename(install_dir())
        current = version_file.read_text().strip() if version_file.is_file() else None
        if local_pwsh().is_file() and current == VERSION:
            return str(local_pwsh())
        key = platform_key()
        if not local_pwsh().is_file():
            console.warn(f"pwsh is not installed. pdt downloads PowerShell {VERSION} "
                         f"(~100 MB) to {console.value(install_dir())}.")
            console.say("Deleting that folder uninstalls it again.")
        elif current:
            console.status(f"Updating PowerShell {console.value(current)} to {console.value(VERSION)} "
                           f"(~100 MB) in {console.value(install_dir())}.")
        else:
            console.status(f"Updating PowerShell to {console.value(VERSION)} "
                           f"(~100 MB) in {console.value(install_dir())}.")
        download_pwsh(key)
    if not local_pwsh().is_file():
        raise PwshError(f"the unpacked archive has no {local_pwsh()}")
    return str(local_pwsh())


def download_pwsh(key: str) -> None:
    url = (f"https://github.com/PowerShell/PowerShell/releases/download/"
           f"v{VERSION}/{archive_name(key)}")
    # Path.rename fails across file systems, and the default temp folder can be on another one.
    new = install_dir().with_name(f"{install_dir().name}.new")
    previous = install_dir().with_name(f"{install_dir().name}.previous")
    shutil.rmtree(new, ignore_errors=True)
    try:
        fetch_verified(url, CHECKSUMS[key], new, PwshError)
        (new / "VERSION").write_text(VERSION)
        if install_dir().exists():
            shutil.rmtree(previous, ignore_errors=True)
            try:
                install_dir().rename(previous)
            except OSError as e:
                raise PwshError(f"cannot replace {install_dir()}: {e.strerror}; close each "
                                f"program that runs from it and try again")
        try:
            new.rename(install_dir())
        except OSError as e:
            if previous.exists():
                try:
                    previous.rename(install_dir())
                except OSError:
                    raise PwshError(f"cannot install to {install_dir()}: {e.strerror}; "
                                    f"the current copy is in {previous}")
            raise PwshError(f"cannot install to {install_dir()}: {e.strerror}")
        if os.name != "nt":
            local_pwsh().chmod(0o755)
        shutil.rmtree(previous, ignore_errors=True)
    finally:
        shutil.rmtree(new, ignore_errors=True)

if __name__ == "__main__":
    with contextlib.redirect_stdout(sys.stderr):
        try:
            path = ensure_pwsh()
        except PwshError as e:
            console.error(console.escape(str(e)))
            sys.exit(1)
    sys.stdout.write(path + "\n")
