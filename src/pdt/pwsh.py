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
import tempfile
from pathlib import Path

from pdt import console
from pdt.config import data_home, machine_data_home, unsaved
from pdt.download import fetch_verified

VERSION = "7.6.8"
CHECKSUMS = {
    "linux-x64": "59112e38ad83350e4f6dc0195953c66b5926792f300ca2e7691b8242e7a16e7f",
    "linux-arm64": "3179e07fd98f026d39c730ebcb7626d071aac3351b171dda13f9f38d99efcef0",
    "osx-x64": "40a4eea4645c87149421cdef978efdcb88e561c96b22d3bfafe9a99ab7f8711d",
    "osx-arm64": "c16a3f433966c6fd5a9c8a660a95c92cb5dbc4b1813416501d1ba30ca160da32",
    "win-x64": "b9f074f923c499b51ec99d313da8e9ae90d17a9f3adf00c73d6189f34d908528",
    "win-arm64": "e889c9bfa6c3a9796bd6268cbeeb389e3968f77c6db46cb282178274d71c160b",
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
    version_file = install_dir() / "VERSION"
    current = version_file.read_text().strip() if version_file.is_file() else None
    if local_pwsh().is_file() and (current == VERSION or unsaved() is not None):
        return str(local_pwsh())
    if unsaved() is not None:
        raise PwshError("pwsh is not installed, and a plan installs nothing; run the deploy "
                        "without --plan to install it")
    key = platform_key()
    if not local_pwsh().is_file():
        console.warn(f"pwsh is not installed. pdt downloads PowerShell {VERSION} "
                     f"(~100 MB) to {console.value(install_dir())}.")
        console.say("Deleting that folder uninstalls it again.")
    elif current:
        console.warn(f"Updating PowerShell {console.value(current)} to {console.value(VERSION)} "
                     f"(~100 MB) in {console.value(install_dir())}.")
    else:
        console.warn(f"Updating PowerShell to {console.value(VERSION)} "
                     f"(~100 MB) in {console.value(install_dir())}.")
    download_pwsh(key)
    if not local_pwsh().is_file():
        raise PwshError(f"the unpacked archive has no {local_pwsh()}")
    return str(local_pwsh())


def download_pwsh(key: str) -> None:
    url = (f"https://github.com/PowerShell/PowerShell/releases/download/"
           f"v{VERSION}/{archive_name(key)}")
    install_dir().parent.mkdir(parents=True, exist_ok=True)
    # The stage sits beside the install, so each move below is a rename.
    stage = Path(tempfile.mkdtemp(prefix="pdt-pwsh-", dir=install_dir().parent))
    try:
        fetch_verified(url, CHECKSUMS[key], stage / "new", PwshError)
        (stage / "new" / "VERSION").write_text(VERSION)
        if install_dir().exists():
            try:
                install_dir().rename(stage / "current")
            except OSError as e:
                raise PwshError(f"cannot replace {install_dir()}: {e.strerror}; close each "
                                f"program that runs from it and try again")
        (stage / "new").rename(install_dir())
        if os.name != "nt":
            local_pwsh().chmod(0o755)
    finally:
        shutil.rmtree(stage, ignore_errors=True)


if __name__ == "__main__":
    with contextlib.redirect_stdout(sys.stderr):
        try:
            path = ensure_pwsh()
        except PwshError as e:
            console.error(console.escape(str(e)))
            sys.exit(1)
    sys.stdout.write(path + "\n")
