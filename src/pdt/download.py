"""Download a pinned archive, verify its sha256, and unpack it.

`gcloud_sdk.py` and `pwsh.py` install their tools this way; each keeps its
own version, checksum table, and final folder.
"""

from __future__ import annotations

import hashlib
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

from pdt import console


def fetch_verified(url: str, sha256: str, stage: Path, error) -> Path:
    """Unpack the archive at `url` into `stage` and return `stage`. Raises `error`
    when the download fails or its sha256 is not `sha256`."""
    console.status(f"downloading {console.value(url)}")
    digest = hashlib.sha256()
    tmp = tempfile.NamedTemporaryFile(suffix=Path(url).suffix, delete=False)
    tmp_path = Path(tmp.name)
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
                if total and sys.stdout.isatty():
                    console.progress(f"  {done // 2**20} / {total // 2**20} MB")
        if sys.stdout.isatty():
            console.say()
        else:
            console.status(f"downloaded {console.value(Path(url).name)}")
        if digest.hexdigest() != sha256:
            raise error(
                f"checksum mismatch for {url}\n"
                f"  expected {sha256}\n"
                f"  got      {digest.hexdigest()}\n"
                f"A newer release may have replaced the pinned one; update VERSION "
                f"and CHECKSUMS in {error.__module__.replace('.', '/')}.py as its "
                f"docstring describes")
        console.status(f"unpacking {console.value(Path(url).name)}")
        if url.endswith(".zip"):
            with zipfile.ZipFile(tmp_path) as archive:
                archive.extractall(stage)
        else:
            with tarfile.open(tmp_path) as tar:
                tar.extractall(stage, filter="data")
    except urllib.error.URLError as e:
        raise error(f"download failed: {e.reason}")
    finally:
        tmp_path.unlink(missing_ok=True)
    return stage
