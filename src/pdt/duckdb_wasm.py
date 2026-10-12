"""Download the pinned DuckDB-WASM files the `pdt gui` SQL workbench runs in the browser.

The workbench page queries a run's CSV files inside the browser, so the
server never needs DuckDB itself and no CDN is touched while a page is
open. The files come from jsdelivr once, are checked against the sha256
below, and land in the user's data folder rather than beside this file,
so upgrading pdt keeps them; deleting DIR removes them, and the next
`pdt gui` downloads them again. A download that fails leaves nothing
behind and the server still starts; the workbench page then says so.

DuckDB's browser module imports `apache-arrow` by bare name, which a
browser cannot resolve, and the package ships no bundle that holds it.
The page therefore also loads Arrow's UMD script and maps the bare name
to `gui/static/arrow.mjs`, which re-exports the four Arrow names the
DuckDB module uses.

To bump the pin, take the tag from https://www.npmjs.com/package/@duckdb/duckdb-wasm
(DuckDB publishes its dev builds as `latest`), download the files under
`dist/` named below, record each file's sha256, and check the module's
`import ... from"apache-arrow"` names still match `arrow.mjs`. Bump
ARROW_VERSION when the package's `apache-arrow` dependency moves.
"""

from __future__ import annotations

import hashlib
import tempfile
import urllib.request
from pathlib import Path

from pdt import console
from pdt.config import data_home

VERSION = "1.33.1-dev57.0"
ARROW_VERSION = "17.0.0"
DUCKDB_URL = f"https://cdn.jsdelivr.net/npm/@duckdb/duckdb-wasm@{VERSION}/dist/"
ARROW_URL = f"https://cdn.jsdelivr.net/npm/apache-arrow@{ARROW_VERSION}/"
FILES = {
    "duckdb-browser.mjs": (
        DUCKDB_URL, "95ea0678ebf4a817464f81ec07c9c4188470f361967975ef6ca85fd674cfc6ec", 32000),
    "duckdb-eh.wasm": (
        DUCKDB_URL, "3abdec74989dcc54d2f2ea5621f611f3c45db1e7dff2f408476014d82beb2029", 35913747),
    "duckdb-browser-eh.worker.js": (
        DUCKDB_URL, "fa889e6068c40426dea67c08cf16ce0cad7404eae94f6a2522adcabb5898eb93", 773223),
    "Arrow.es2015.min.js": (
        ARROW_URL, "375fb7c891e25677c021e585d4ec51a5752112b182f41d51071b8401e590379e", 172843),
}
CONTENT_TYPES = {".wasm": "application/wasm", ".mjs": "text/javascript",
                 ".js": "text/javascript"}

DIR = data_home() / "pdt" / "duckdb-wasm" / VERSION


class DuckdbWasmError(Exception):
    pass


def installed() -> bool:
    return all((DIR / name).is_file() for name in FILES)


def ensure() -> Path:
    """DIR, with every pinned file in it; downloads the ones that are missing."""
    DIR.mkdir(parents=True, exist_ok=True)
    for name, (base, checksum, size) in FILES.items():
        if not (DIR / name).is_file():
            console.status(f"downloading DuckDB for the browser, {console.value(name)}, "
                           f"{size_text(size)}, once")
            download(base + name, name, checksum)
    return DIR


def size_text(count: int) -> str:
    return f"{count / 2**20:.0f} MB" if count >= 2**20 else f"{count // 2**10} KB"


def download(url: str, name: str, checksum: str) -> None:
    digest = hashlib.sha256()
    tmp = tempfile.NamedTemporaryFile(dir=DIR, prefix=name + ".", delete=False)
    tmp_path = Path(tmp.name)
    try:
        with tmp, urllib.request.urlopen(url, timeout=60) as resp:
            while True:
                chunk = resp.read(1024 * 1024)
                if chunk == b"":
                    break
                digest.update(chunk)
                tmp.write(chunk)
        if digest.hexdigest() != checksum:
            raise DuckdbWasmError(
                f"checksum mismatch for {url}\n"
                f"  expected {checksum}\n"
                f"  got      {digest.hexdigest()}\n"
                f"A newer build may have replaced the pinned one; update VERSION and "
                f"FILES in pdt/duckdb_wasm.py")
        tmp_path.replace(DIR / name)
    except OSError as e:
        raise DuckdbWasmError(f"could not download {url}: {getattr(e, 'reason', e)}")
    finally:
        tmp_path.unlink(missing_ok=True)
