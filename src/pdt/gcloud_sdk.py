"""Find gcloud, or install a pinned Google Cloud CLI in the pdt data folder.

Search order: gcloud on PATH, then the local install. If neither exists,
download the pinned version below (~150 MB) from dl.google.com, verify its
sha256, and unpack it, with no question, the same way uv installs awscli
and azure-cli for the other providers. No admin rights are needed; deleting
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

import os
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from pdt import console
from pdt.config import data_home
from pdt.download import fetch_verified

VERSION = "581.0.0"
CHECKSUMS = {
    "linux-x86_64": "deffdbe82ca6e3d19ffb291d063a651488e04e1b33799b5a238e4b5c6784e3c6",
    "linux-arm": "22cfc09888525c6daadb8764388ce14e6c26baf80ab07938eacb08c2b4ae64c9",
    "darwin-x86_64": "af6082b38fb34603c88c93c7d1a7b222d8d202c5412413232f4a7e46db97728d",
    "darwin-arm": "8b5d7b14439ce51dc63aaacb2f7f18e5765db437f7b6b177cbc7260889a91e56",
    "windows-x86_64": "4ba8775a6fef8e09f9013c711e5a816fd6ce68c8f17da642141f24fd92891530",
}

SDK_DIR = data_home() / "pdt" / "gcloud"
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


# pdt pins its gcloud release, so the update nag cannot be acted on.
def quiet_notices() -> None:
    os.environ.setdefault("CLOUDSDK_COMPONENT_MANAGER_DISABLE_UPDATE_CHECK", "true")
    os.environ.setdefault("CLOUDSDK_SURVEY_DISABLE_PROMPTS", "true")


def ensure_gcloud() -> str:
    quiet_notices()
    found = shutil.which("gcloud")
    if found:
        return found
    if LOCAL_GCLOUD.is_file():
        set_sdk_python()
        return str(LOCAL_GCLOUD)
    key = sdk_platform()
    console.status(f"Installing the Google Cloud CLI {VERSION} (~150 MB) to "
                   f"{console.value(SDK_DIR)}. This happens once and can take several minutes...")
    console.say("Deleting that folder uninstalls it again.")
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
    stage = Path(tempfile.mkdtemp(prefix="pdt-gcloud-"))
    try:
        fetch_verified(url, CHECKSUMS[key], stage, GcloudError)
        if SDK_DIR.exists():
            shutil.rmtree(SDK_DIR)
        SDK_DIR.mkdir(parents=True)
        shutil.move(str(stage / "google-cloud-sdk"), str(SDK_DIR / "google-cloud-sdk"))
    finally:
        shutil.rmtree(stage, ignore_errors=True)


if __name__ == "__main__":
    try:
        path = ensure_gcloud()
    except GcloudError as e:
        console.error(console.escape(str(e)))
        sys.exit(1)
    console.say(console.value(path))
    subprocess.run([path, "--version"])
