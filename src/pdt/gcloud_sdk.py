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

import contextlib
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

from pdt import console
from pdt.config import data_home, locked, unsaved
from pdt.download import fetch_verified

VERSION = "594.0.0"
CHECKSUMS = {
    "linux-x86_64": "1f7233537c26bba94a3ea3f9085a60396745eaf44b21b8454fc6fdd15bb6fcde",
    "linux-arm": "e58fb86fff3dfd7101e1ff138f06bd4e338c7bf12a7508a7871985e7ea886275",
    "darwin-x86_64": "f8c788cb2ce0af3bd03f236d67c825afe810908f4cd1ade435eb7b5bfb4173c7",
    "darwin-arm": "45fbcc5a2caf9ec43847383d2f9fe4720eb8a650349be8d9bd271bdfb6912b55",
    "windows-x86_64": "17359246fa68be3a6681569988040044f60f07ed784b1e1d7b75893b941762b3",
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
    # Another process can move the install away between the check and the read.
    with contextlib.suppress(FileNotFoundError):
        if LOCAL_GCLOUD.is_file() and SDK_VERSION.read_text().strip() == VERSION:
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
