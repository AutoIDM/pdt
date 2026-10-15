#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["pdt-cli[apps]==PDT_VERSION"]
# ///
"""Intune Backup runs IntuneManagement against an Intune tenant.

IntuneManagement (https://github.com/Micke-K/IntuneManagement) is not on the
PowerShell Gallery, so this file downloads the release pinned below from
GitHub into .pdt/intune-management/ in this folder: once on a computer, and
at the start of each run of a deployed job. Then run.ps1 signs in and does
the work.

The last good backup is kept in the app's data store, under state/backup/,
so each run can report what changed since the one before. A run that fails
leaves it as it was.

Exit codes: 0 ok, 1 bad config or a failed run.ps1, 2 download failure.
"""

from __future__ import annotations

import base64
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from pdt import config
from pdt.pwsh import ensure_pwsh
from pdt.utils import storage
from pdt.utils.log import die, log

# The IntuneManagement release this app runs. To move to another release, copy
# its tag and the full commit id it points at from the project's Releases page.
# 4.0 is the first release that runs on Linux, where the cloud jobs run.
VERSION = "4.0.0-beta1"
COMMIT = "7869619510e97bb7f482acd44789bb35d5b8e807"
ARCHIVE = "https://github.com/Micke-K/IntuneManagement/archive/{commit}.tar.gz"

# What the module needs with no desktop UI: its scripts, and the MSAL library
# it signs in with on PowerShell 7. The UI folders (60 MB) stay out.
MODULE_PARTS = ("IntuneManagement.psd1", "IntuneManagement.psm1", "LICENSE",
                "Classes/", "Config/", "Internal/", "Public/", "Bin/MSAL_PS7/")

EXIT_CONFIG = 1
EXIT_DOWNLOAD = 2


def fetch_module(folder: Path, url: str = "") -> None:
    """Put the pinned IntuneManagement release in `folder`, unless it is there already.
    The RELEASE file in it names the commit it holds."""
    marker = folder / "RELEASE"
    if marker.is_file() and marker.read_text().strip() == COMMIT:
        return
    url = url or ARCHIVE.format(commit=COMMIT)
    log("info", "downloading IntuneManagement", version=VERSION, url=url)
    folder.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=folder.parent) as tmp:
        stage = Path(tmp) / "module"
        try:
            with urllib.request.urlopen(url, timeout=120) as resp, \
                    tarfile.open(fileobj=resp, mode="r|gz") as archive:
                for member in archive:
                    # Every path starts with the archive's own top folder.
                    _top, _, inside = member.name.partition("/")
                    if not any(inside == part or (part.endswith("/") and inside.startswith(part))
                               for part in MODULE_PARTS):
                        continue
                    member.name = inside
                    archive.extract(member, stage, filter="data")
        except (urllib.error.URLError, OSError, tarfile.TarError) as e:
            die(EXIT_DOWNLOAD, "could not download IntuneManagement", url=url, error=str(e))
        if not (stage / "IntuneManagement.psd1").is_file():
            die(EXIT_DOWNLOAD, "the IntuneManagement download holds no IntuneManagement.psd1", url=url)
        (stage / "RELEASE").write_text(COMMIT + "\n")
        shutil.rmtree(folder, ignore_errors=True)
        stage.rename(folder)


def certificate_env(app_dir: Path, folder: Path) -> dict:
    """PDT_AZURE_PRIVATE_KEY_PATH as the full path of the .pfx file, or {} when the app signs
    in with a secret. A certificate in PDT_AZURE_PRIVATE_KEY_B64 is written into `folder`
    first, and wins over PDT_AZURE_PRIVATE_KEY_PATH."""
    b64 = os.environ.get("PDT_AZURE_PRIVATE_KEY_B64", "").strip()
    if b64 != "":
        try:
            data = base64.b64decode(b64, validate=True)
        except ValueError:
            die(EXIT_CONFIG, "PDT_AZURE_PRIVATE_KEY_B64 is not valid base64")
        path = folder / "certificate.pfx"
        path.write_bytes(data)
        return {"PDT_AZURE_PRIVATE_KEY_PATH": str(path)}
    raw = os.environ.get("PDT_AZURE_PRIVATE_KEY_PATH", "").strip()
    if raw == "":
        return {}
    path = Path(raw).expanduser()
    if not path.is_absolute():
        # The same folders `pdt deploy` looks in: the .env files', the app's, the project's.
        bases = [env_file.parent for env_file in config.find_env_files(app_dir)]
        bases += [app_dir, config.find_project(app_dir)]
        path = next((base / path for base in bases if (base / path).is_file()), path)
    if not path.is_file():
        die(EXIT_CONFIG, "the certificate file in PDT_AZURE_PRIVATE_KEY_PATH does not exist",
            path=str(path))
    return {"PDT_AZURE_PRIVATE_KEY_PATH": str(path)}


def main() -> int:
    app_dir = Path(__file__).resolve().parent
    try:
        app = config.merged_app(app_dir.name)
        config.load_env(app_dir)
    except config.ConfigError as e:
        die(EXIT_CONFIG, "config is not valid", problem=str(e))
    missing = config.missing_env(app)
    if missing != "":
        die(EXIT_CONFIG, f"env vars missing: {missing}")
    pwsh = ensure_pwsh()
    # The data store's lock is held from here, so two runs never share a download or a backup.
    with storage.sync() as run, tempfile.TemporaryDirectory(prefix="intune-backup-") as tmp:
        fetch_module(app_dir / ".pdt" / "intune-management")
        last = run.state / "backup"
        env = {**os.environ, **certificate_env(app_dir, Path(tmp)), "PDT_OUTPUT_DIR": str(run.output)}
        code = subprocess.run(
            [pwsh, "-NoProfile", "-NonInteractive", "-File", str(app_dir / "run.ps1"),
             "-LastBackup", str(last)],
            cwd=app_dir, env=env, stdin=subprocess.DEVNULL).returncode
        if code != 0:
            log("error", "run.ps1 failed; the next run compares with the last good backup",
                exit_code=code)
            raise SystemExit(code)
        shutil.rmtree(last, ignore_errors=True)
        shutil.copytree(run.output / "backup", last)
    return 0


if __name__ == "__main__":
    sys.exit(main())
