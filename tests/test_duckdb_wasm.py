import hashlib
import io

import pytest

from pdt import duckdb_wasm

CONTENT = {name: f"content of {name}".encode() for name in duckdb_wasm.FILES}


class FakeResponse(io.BytesIO):
    def __init__(self, data):
        super().__init__(data)
        self.headers = {"Content-Length": str(len(data))}


@pytest.fixture
def pinned(monkeypatch, tmp_path):
    monkeypatch.setattr(duckdb_wasm, "DIR", tmp_path / "duckdb-wasm")
    monkeypatch.setattr(duckdb_wasm, "FILES", {
        name: (base, hashlib.sha256(CONTENT[name]).hexdigest(), len(CONTENT[name]))
        for name, (base, _checksum, _size) in duckdb_wasm.FILES.items()})
    fetched = []

    def fake_urlopen(url, timeout=0):
        fetched.append(url)
        return FakeResponse(CONTENT[url.rsplit("/", 1)[-1]])

    monkeypatch.setattr(duckdb_wasm.urllib.request, "urlopen", fake_urlopen)
    return fetched


def test_ensure_downloads_every_pinned_file_and_checks_its_hash(pinned):
    folder = duckdb_wasm.ensure()
    assert folder == duckdb_wasm.DIR
    for name, (base, checksum, _size) in duckdb_wasm.FILES.items():
        assert base + name in pinned
        assert hashlib.sha256((folder / name).read_bytes()).hexdigest() == checksum
    assert duckdb_wasm.installed()
    assert sorted(path.name for path in folder.iterdir()) == sorted(duckdb_wasm.FILES)


def test_ensure_skips_a_file_that_is_already_there(pinned):
    duckdb_wasm.DIR.mkdir(parents=True)
    (duckdb_wasm.DIR / "duckdb-eh.wasm").write_bytes(CONTENT["duckdb-eh.wasm"])
    assert not duckdb_wasm.installed()
    duckdb_wasm.ensure()
    assert all(not url.endswith("duckdb-eh.wasm") for url in pinned)
    assert len(pinned) == len(duckdb_wasm.FILES) - 1


def test_a_bad_checksum_fails_and_leaves_nothing_behind(pinned, monkeypatch):
    monkeypatch.setitem(CONTENT, "duckdb-eh.wasm", b"something else")
    with pytest.raises(duckdb_wasm.DuckdbWasmError, match="checksum mismatch"):
        duckdb_wasm.ensure()
    assert not (duckdb_wasm.DIR / "duckdb-eh.wasm").exists()
    assert not [path for path in duckdb_wasm.DIR.iterdir() if "wasm" in path.name]
    assert not duckdb_wasm.installed()
