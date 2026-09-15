"""Find rclone, or install a pinned copy in the pdt data folder.

`pdt storage <app> mount` shows the app's folder in the store as a folder on
this computer. rclone does the work on every provider and every OS, so pdt
manages one tool instead of one per cloud. The search order and the install
follow `pdt.gcloud_sdk`: rclone on PATH, then the local install, then one
[y/N] question and a checked download from downloads.rclone.org.

Windows mounts need WinFsp, a driver rclone talks to. pdt downloads the
pinned installer after the same kind of question and runs it; Windows asks
for administrator approval.

To bump the pins, take the checksums from
https://downloads.rclone.org/v<VERSION>/SHA256SUMS and the WinFsp digest from
the release page at https://github.com/winfsp/winfsp/releases.
"""

from __future__ import annotations

import hashlib
import os
import platform
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

from pdt import console
from pdt.config import data_home

VERSION = "1.75.1"
CHECKSUMS = {
    "linux-amd64": "982b5aa772841168f8e380f139e9e787b2a105403e32b94da8676a0e1c0a13ab",
    "linux-arm64": "03f2504174034b6d004152ed7369251c9a9ec1f7e0836eda420f5c7a5ec0dff9",
    "osx-amd64": "29253d0288b8fbbac46baad6e5f6add6cb01d462c79f10805bbd4631c4cdf82c",
    "osx-arm64": "c61d7a371c62bcbbe882c3423aa4b8bf63485c248dd0f692997b8f0c3f6d0c6f",
    "windows-amd64": "200eb602c126d82aa38b51e0f6b9ae837473ff99b51278d3f6f837574c494d6e",
}
WINFSP_VERSION = "2.1.25156"
WINFSP_URL = f"https://github.com/winfsp/winfsp/releases/download/v2.1/winfsp-{WINFSP_VERSION}.msi"
WINFSP_CHECKSUM = "073a70e00f77423e34bed98b86e600def93393ba5822204fac57a29324db9f7a"
WINFSP_DLL = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "WinFsp"

RCLONE_DIR = data_home() / "pdt" / "rclone"
RCLONE_BIN = "rclone.exe" if os.name == "nt" else "rclone"
LOCAL_RCLONE = RCLONE_DIR / RCLONE_BIN
INSTALL_DOCS = "https://rclone.org/install/"


class RcloneError(Exception):
    pass


def build_key() -> str:
    system = platform.system().lower()
    machine = platform.machine().lower()
    arch = "arm64" if machine in ("arm64", "aarch64") else "amd64"
    os_name = {"linux": "linux", "darwin": "osx", "windows": "windows"}.get(system)
    if os_name is None or (os_name == "windows" and arch != "amd64"):
        raise RcloneError(
            f"no pinned rclone for {platform.system()} {platform.machine()}; "
            f"install it yourself from {INSTALL_DOCS}")
    return f"{os_name}-{arch}"


def ensure_rclone(assume_yes: bool = False) -> str:
    found = shutil.which("rclone")
    if found:
        return found
    if LOCAL_RCLONE.is_file():
        return str(LOCAL_RCLONE)
    key = build_key()
    console.warn(f"rclone is not installed. pdt can download rclone {VERSION} "
                 f"(~25 MB) to {RCLONE_DIR}.")
    console.say("Deleting that folder uninstalls it again.")
    if not agreed(assume_yes):
        raise RcloneError(
            f"rclone is required to mount; answer y to download it, or install it "
            f"yourself from {INSTALL_DOCS}")
    name = f"rclone-v{VERSION}-{key}"
    archive = download(f"https://downloads.rclone.org/v{VERSION}/{name}.zip", CHECKSUMS[key])
    try:
        with zipfile.ZipFile(archive) as zipped:
            RCLONE_DIR.mkdir(parents=True, exist_ok=True)
            with zipped.open(f"{name}/{RCLONE_BIN}") as src, open(LOCAL_RCLONE, "wb") as dst:
                shutil.copyfileobj(src, dst)
    finally:
        archive.unlink(missing_ok=True)
    LOCAL_RCLONE.chmod(0o755)
    return str(LOCAL_RCLONE)


def ensure_winfsp(assume_yes: bool = False) -> None:
    if os.name != "nt" or WINFSP_DLL.is_dir():
        return
    console.warn(f"Mounting on Windows needs WinFsp. pdt can download WinFsp "
                 f"{WINFSP_VERSION} (~3 MB) and install it; Windows asks for approval.")
    if not agreed(assume_yes):
        raise RcloneError("WinFsp is required to mount; answer y to install it, or "
                          "install it yourself from https://winfsp.dev/rel/")
    installer = download(WINFSP_URL, WINFSP_CHECKSUM)
    try:
        proc = subprocess.run(["msiexec", "/i", str(installer), "/passive", "/norestart"],
                              check=False)
    finally:
        installer.unlink(missing_ok=True)
    if proc.returncode:
        raise RcloneError(f"the WinFsp installer ended with code {proc.returncode}")


def agreed(assume_yes: bool) -> bool:
    if assume_yes:
        return True
    try:
        return input("Download now? [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def download(url: str, checksum: str) -> Path:
    console.status(f"downloading {url}")
    digest = hashlib.sha256()
    tmp = tempfile.NamedTemporaryFile(suffix=Path(url).suffix, delete=False)
    path = Path(tmp.name)
    try:
        with tmp, urllib.request.urlopen(url, timeout=60) as resp:
            while True:
                chunk = resp.read(1024 * 1024)
                if chunk == b"":
                    break
                digest.update(chunk)
                tmp.write(chunk)
    except urllib.error.URLError as e:
        path.unlink(missing_ok=True)
        raise RcloneError(f"download failed: {e.reason}")
    if digest.hexdigest() != checksum:
        path.unlink(missing_ok=True)
        raise RcloneError(
            f"checksum mismatch for {url}\n"
            f"  expected {checksum}\n"
            f"  got      {digest.hexdigest()}\n"
            f"A newer release may have replaced the pinned one; update the pins "
            f"in pdt/rclone.py")
    return path
