"""A task runs as SYSTEM, so it cannot use the uv in the deploying user's
profile, and an uninstall there would break every task.

To bump the pin, take the new version number and the two checksums from
the `uv-<arch>-pc-windows-msvc.zip.sha256` assets of the release at
https://github.com/astral-sh/uv/releases
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path, PureWindowsPath

from pdt.config import data_home
from pdt.download import fetch_verified

VERSION = "0.13.1"
# The Python of deploy_common.BASE_IMAGE, so a Windows job gets the same Python as a cloud job,
# and wheels exist for it (the newest Python can lack them on Windows ARM64).
PYTHON = "3.12"
CHECKSUMS = {
    "x86_64": "3b2661a463a5baea7f5076ea2b25ab71d77f36a6c0d809e5350651792f80a672",
    "aarch64": "ec7846a5939b4654afee24f2d82bf6fde71bd7a6952d7748f1135ab301dd0a49",
}
UV_ARCH_BY_PROCESSOR = {"AMD64": "x86_64", "ARM64": "aarch64"}


class UvError(Exception):
    pass


def machine_uv(program_data: str) -> PureWindowsPath:
    return PureWindowsPath(program_data) / "pdt" / "bin" / "uv.exe"


def machine_cache(program_data: str) -> PureWindowsPath:
    return PureWindowsPath(program_data) / "pdt" / "uv-cache"


def fetch(processor: str) -> Path:
    arch = UV_ARCH_BY_PROCESSOR.get(processor.upper())
    if arch is None:
        raise UvError(f"pdt has no uv for a Windows PC with a {processor} processor")
    target = data_home() / "pdt" / "uv-windows" / VERSION / arch / "uv.exe"
    if target.is_file():
        return target
    url = (f"https://github.com/astral-sh/uv/releases/download/{VERSION}/"
           f"uv-{arch}-pc-windows-msvc.zip")
    stage = Path(tempfile.mkdtemp(prefix="pdt-uv-"))
    try:
        fetch_verified(url, CHECKSUMS[arch], stage, UvError)
        found = next(stage.rglob("uv.exe"), None)
        if found is None:
            raise UvError(f"the archive {url} holds no uv.exe")
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_name(f"uv.exe.{os.getpid()}.partial")
        shutil.copy(found, partial)
        os.replace(partial, target)
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    return target
