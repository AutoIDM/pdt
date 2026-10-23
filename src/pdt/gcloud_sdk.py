"""Find gcloud, or install a pinned Google Cloud CLI in the pdt data folder.

Search order: gcloud on PATH, then the local install. If neither exists,
download the pinned version below (~150 MB) from dl.google.com, verify its
sha256, and unpack it, with no question, the same way uv installs awscli
and azure-cli for the other providers. No admin rights are needed; deleting
SDK_DIR removes the install. The download lives in the user's data folder
rather than beside this file, so upgrading pdt keeps it. Login state lives
in ~/.config/gcloud either way, so a later system install keeps working.
A local install of another version (the SDK's own VERSION file) is
replaced the same way, and the current one stays until the new one is
unpacked.

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
from pathlib import Path

from pdt import console
from pdt.config import data_home, locked, unsaved
from pdt.download import fetch_verified

VERSION = "590.0.1"
CHECKSUMS = {
    "linux-x86_64": "d1ae7be7e12c4e7d8286fab4774f5441020aca89dc7793ddacbace8d11d20f06",
    "linux-arm": "fd760d8b1c2c4c484d7b659964994b07aaefddc6ac4350e681940477dbdbb22c",
    "darwin-x86_64": "32e41cdb54d3946bcd5d29bd2bd9f3d8923bf1578e8ecc94d0999f2f18f2f0f1",
    "darwin-arm": "02f20618f43b9147e049a73fec37ab71b586b0dc6a242fa6ca835229cb5e542f",
    "windows-x86_64": "38dad2c60c8507a2042e419ac9293e66b9f96156226c6cda92dcb1c4e8c65cf2",
}

SDK_DIR = data_home() / "pdt" / "gcloud"
GCLOUD_BIN = "gcloud.cmd" if os.name == "nt" else "gcloud"
LOCAL_GCLOUD = SDK_DIR / "google-cloud-sdk" / "bin" / GCLOUD_BIN
SDK_VERSION = SDK_DIR / "google-cloud-sdk" / "VERSION"
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
    previous = SDK_DIR.with_name(f"{SDK_DIR.name}.previous")
    if unsaved() is not None:
        for gcloud in (LOCAL_GCLOUD, previous / "google-cloud-sdk" / "bin" / GCLOUD_BIN):
            if gcloud.is_file():
                set_sdk_python()
                return str(gcloud)
        raise GcloudError("the Google Cloud CLI is not installed, and a plan installs nothing; "
                          "run the deploy without --plan to install it")
    if LOCAL_GCLOUD.is_file() and SDK_VERSION.is_file() and SDK_VERSION.read_text().strip() == VERSION:
        set_sdk_python()
        return str(LOCAL_GCLOUD)
    SDK_DIR.parent.mkdir(parents=True, exist_ok=True)
    with locked(SDK_DIR):
        if previous.is_dir() and not SDK_DIR.exists():
            previous.rename(SDK_DIR)
        current = SDK_VERSION.read_text().strip() if SDK_VERSION.is_file() else None
        if LOCAL_GCLOUD.is_file() and current == VERSION:
            set_sdk_python()
            return str(LOCAL_GCLOUD)
        key = sdk_platform()
        if not LOCAL_GCLOUD.is_file():
            console.status(f"Installing the Google Cloud CLI {VERSION} (~150 MB) to "
                           f"{console.value(SDK_DIR)}. This happens once and can take several minutes...")
            console.say("Deleting that folder uninstalls it again.")
        elif current:
            console.status(f"Updating the Google Cloud CLI {console.value(current)} to {console.value(VERSION)} "
                           f"(~150 MB) in {console.value(SDK_DIR)}. This can take several minutes...")
        else:
            console.status(f"Updating the Google Cloud CLI to {console.value(VERSION)} "
                           f"(~150 MB) in {console.value(SDK_DIR)}. This can take several minutes...")
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
    # Path.rename fails across file systems, and the default temp folder can be on another one.
    new = SDK_DIR.with_name(f"{SDK_DIR.name}.new")
    previous = SDK_DIR.with_name(f"{SDK_DIR.name}.previous")
    shutil.rmtree(new, ignore_errors=True)
    try:
        fetch_verified(url, CHECKSUMS[key], new, GcloudError)
        if SDK_DIR.exists():
            shutil.rmtree(previous, ignore_errors=True)
            try:
                SDK_DIR.rename(previous)
            except OSError as e:
                raise GcloudError(f"cannot replace {SDK_DIR}: {e.strerror}; close each "
                                  f"program that runs from it and try again")
        try:
            new.rename(SDK_DIR)
        except OSError as e:
            if previous.exists():
                try:
                    previous.rename(SDK_DIR)
                except OSError:
                    raise GcloudError(f"cannot install to {SDK_DIR}: {e.strerror}; "
                                      f"the current copy is in {previous}")
            raise GcloudError(f"cannot install to {SDK_DIR}: {e.strerror}")
        shutil.rmtree(previous, ignore_errors=True)
    finally:
        shutil.rmtree(new, ignore_errors=True)

if __name__ == "__main__":
    try:
        path = ensure_gcloud()
    except GcloudError as e:
        console.error(console.escape(str(e)))
        sys.exit(1)
    console.say(console.value(path))
    subprocess.run([path, "--version"])
