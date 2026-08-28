"""Find gcloud, or install a pinned Google Cloud CLI in the pdt data folder.

Search order: gcloud on PATH, then the local install. If neither exists,
offer to download the pinned version below (~150 MB) from dl.google.com,
verify its sha256, and unpack it. No admin rights are needed; deleting
SDK_DIR removes the install. The download lives in the user's data folder
rather than beside this file, so upgrading pdt keeps it. Login state lives
in ~/.config/gcloud either way, so a later system install keeps working.

Windows uses the bundled-python zip, so the CLI needs no Python there;
the other platforms reuse this interpreter via CLOUDSDK_PYTHON.

To bump the pin, take the new version number and checksums from
https://docs.cloud.google.com/sdk/docs/downloads-versioned-archives
The Windows zip name on that page is versioned, so its checksum applies
directly. The tar.gz names there are the versionless "latest" archives,
and the versioned tar.gz we pin differs in its gzip wrapper: verify the
versionless archive against the documented checksum, confirm `gunzip -c`
of both archives hashes identically, then record the versioned archive's
own sha256 below.
"""

from __future__ import annotations

import hashlib
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

VERSION = "581.0.0"
CHECKSUMS = {
    "linux-x86_64": "deffdbe82ca6e3d19ffb291d063a651488e04e1b33799b5a238e4b5c6784e3c6",
    "linux-arm": "22cfc09888525c6daadb8764388ce14e6c26baf80ab07938eacb08c2b4ae64c9",
    "darwin-x86_64": "af6082b38fb34603c88c93c7d1a7b222d8d202c5412413232f4a7e46db97728d",
    "darwin-arm": "8b5d7b14439ce51dc63aaacb2f7f18e5765db437f7b6b177cbc7260889a91e56",
    "windows-x86_64": "4ba8775a6fef8e09f9013c711e5a816fd6ce68c8f17da642141f24fd92891530",
}

if os.name == "nt":
    _DATA_HOME = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData/Local")
else:
    _DATA_HOME = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")
SDK_DIR = _DATA_HOME / "pdt" / "gcloud"
GCLOUD_BIN = "gcloud.cmd" if os.name == "nt" else "gcloud"
LOCAL_GCLOUD = SDK_DIR / "google-cloud-sdk" / "bin" / GCLOUD_BIN
INSTALL_DOCS = "https://cloud.google.com/sdk/docs/install"


class GcloudError(Exception):
    pass


def sdk_platform() -> str:
    system = platform.system().lower()
    machine = platform.machine().lower()
    if system == "linux":
        return "linux-arm" if machine in ("arm64", "aarch64") else "linux-x86_64"
    if system == "darwin":
        return "darwin-arm" if machine == "arm64" else "darwin-x86_64"
    if system == "windows" and machine in ("amd64", "x86_64"):
        return "windows-x86_64"
    raise GcloudError(
        f"no pinned archive for {platform.system()} {platform.machine()}; "
        f"install the Google Cloud CLI from {INSTALL_DOCS}")


def archive_name(key: str) -> str:
    # Google names the Windows bundled-python zip "sdk", the rest "cli".
    if key == "windows-x86_64":
        return f"google-cloud-sdk-{VERSION}-windows-x86_64-bundled-python.zip"
    return f"google-cloud-cli-{VERSION}-{key}.tar.gz"


def ensure_gcloud(assume_yes: bool = False) -> str:
    found = shutil.which("gcloud")
    if found:
        return found
    if LOCAL_GCLOUD.is_file():
        set_sdk_python()
        return str(LOCAL_GCLOUD)
    key = sdk_platform()
    print(f"gcloud is not installed. pdt can download the Google Cloud CLI "
          f"{VERSION} (~150 MB) to {SDK_DIR}.")
    print("Deleting that folder uninstalls it again.")
    if not assume_yes:
        try:
            answer = input("Download now? [y/N] ").strip().lower()
        except EOFError:
            answer = ""
        if answer not in ("y", "yes"):
            raise GcloudError(
                f"gcloud is required; answer y to download it, or install it "
                f"yourself from {INSTALL_DOCS}")
    download_sdk(key)
    if not LOCAL_GCLOUD.is_file():
        raise GcloudError(f"the unpacked SDK has no {LOCAL_GCLOUD}")
    set_sdk_python()
    return str(LOCAL_GCLOUD)


def set_sdk_python() -> None:
    # The tar.gz SDKs ship no Python; reuse this interpreter. The
    # Windows zip bundles its own, which gcloud.cmd finds by itself.
    if os.name != "nt":
        os.environ.setdefault("CLOUDSDK_PYTHON", sys.executable)


def download_sdk(key: str) -> None:
    url = (f"https://dl.google.com/dl/cloudsdk/channels/rapid/downloads/"
           f"{archive_name(key)}")
    print(f"downloading {url}")
    digest = hashlib.sha256()
    tmp = tempfile.NamedTemporaryFile(suffix=Path(url).suffix, delete=False)
    tmp_path = Path(tmp.name)
    stage = Path(tempfile.mkdtemp(prefix="pdt-gcloud-"))
    try:
        with tmp, urllib.request.urlopen(url, timeout=60) as resp:
            total = int(resp.headers.get("Content-Length") or 0)
            done = 0
            while True:
                chunk = resp.read(1024 * 1024)
                if chunk == b"":
                    break
                digest.update(chunk)
                tmp.write(chunk)
                done += len(chunk)
                if total:
                    print(f"\r  {done // 2**20} / {total // 2**20} MB", end="", flush=True)
        print()
        if digest.hexdigest() != CHECKSUMS[key]:
            raise GcloudError(
                f"checksum mismatch for {url}\n"
                f"  expected {CHECKSUMS[key]}\n"
                f"  got      {digest.hexdigest()}\n"
                f"A newer release may have replaced the pinned one; update VERSION "
                f"and CHECKSUMS in pdt/gcloud_sdk.py from {INSTALL_DOCS}")
        print(f"unpacking to {SDK_DIR}")
        if url.endswith(".zip"):
            with zipfile.ZipFile(tmp_path) as archive:
                archive.extractall(stage)
        else:
            with tarfile.open(tmp_path) as tar:
                tar.extractall(stage, filter="data")
        if SDK_DIR.exists():
            shutil.rmtree(SDK_DIR)
        SDK_DIR.mkdir()
        shutil.move(str(stage / "google-cloud-sdk"), str(SDK_DIR / "google-cloud-sdk"))
    except urllib.error.URLError as e:
        raise GcloudError(f"download failed: {e.reason}")
    finally:
        tmp_path.unlink(missing_ok=True)
        shutil.rmtree(stage, ignore_errors=True)


if __name__ == "__main__":
    try:
        path = ensure_gcloud()
    except GcloudError as e:
        print(f"error: {e}")
        sys.exit(1)
    print(path)
    subprocess.run([path, "--version"])
