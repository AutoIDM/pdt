"""Find pwsh, or install a pinned PowerShell 7 in the pdt data folder.

Search order: pwsh on PATH, then the local install. If neither exists,
download the pinned version below (~70 MB, ~110 MB on Windows) from
github.com, verify its sha256, and unpack it, with no question, the same
way gcloud_sdk.py installs the Google Cloud CLI. On macOS and Linux the
install lives in the user's data folder. On Windows it lives in
%ProgramData%\\pdt\\pwsh, because the scheduled task runs as SYSTEM and
must reach the same pwsh the deploying user installed.

To bump the pin, take the new version number and the five checksums from
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
from pdt.config import data_home, machine_data_home
from pdt.download import fetch_verified

VERSION = "7.6.6"
CHECKSUMS = {
    "linux-x64": "ddbc4a2d113bbd46d283cfedcbcd117a70caefd7673f41f2b4e0000badf103bc",
    "linux-arm64": "924829e54c983648f6f1419a2dc7f9433c861b2fb5bd57736ff096c24f133729",
    "osx-x64": "e325ed9f666894eb39a5ea52800b602da2fb4242bbe9747ceddb39cdc66de805",
    "osx-arm64": "6df833d094ebac1c1a74340d7b3437f4aaf5e03ce640484a1c4359f3ce8b3db1",
    "win-x64": "02fe458be20493fbdf43f61ea20610b811ee6c738ab1676c61b9cfcd1a33c860",
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
    if system == "windows" and machine in ("amd64", "x86_64"):
        return "win-x64"
    raise PwshError(
        f"no pinned archive for {platform.system()} {platform.machine()}; "
        f"install PowerShell 7 from {INSTALL_DOCS}")


def archive_name(key: str) -> str:
    # Microsoft capitalises the Windows zip and not the tar.gz files.
    if key == "win-x64":
        return f"PowerShell-{VERSION}-win-x64.zip"
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
    if local_pwsh().is_file():
        return str(local_pwsh())
    key = platform_key()
    console.warn(f"pwsh is not installed. pdt downloads PowerShell {VERSION} "
                 f"(~100 MB) to {install_dir()}.")
    console.say("Deleting that folder uninstalls it again.")
    download_pwsh(key)
    if not local_pwsh().is_file():
        raise PwshError(f"the unpacked archive has no {local_pwsh()}")
    return str(local_pwsh())


def download_pwsh(key: str) -> None:
    url = (f"https://github.com/PowerShell/PowerShell/releases/download/"
           f"v{VERSION}/{archive_name(key)}")
    stage = Path(tempfile.mkdtemp(prefix="pdt-pwsh-"))
    try:
        fetch_verified(url, CHECKSUMS[key], stage, PwshError)
        if install_dir().exists():
            shutil.rmtree(install_dir())
        install_dir().parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(stage), str(install_dir()))
        if os.name != "nt":
            local_pwsh().chmod(0o755)
    finally:
        shutil.rmtree(stage, ignore_errors=True)


if __name__ == "__main__":
    with contextlib.redirect_stdout(sys.stderr):
        try:
            path = ensure_pwsh()
        except PwshError as e:
            console.error(str(e))
            sys.exit(1)
    sys.stdout.write(path + "\n")
