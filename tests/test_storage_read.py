import json
import os
from pathlib import Path

import pytest

from conftest import add_app
from pdt.utils import storage
from pdt.utils.storage import LOCK, Store


@pytest.fixture
def store(tmp_path):
    folder = tmp_path / "store" / "warehouse"
    folder.mkdir(parents=True)
    return Store(folder.as_uri() + "/")


def write(store, path, text):
    file = store.backend().folder / path
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(text)
    return file


def test_a_local_write_lands_whole_through_a_rename(store, monkeypatch):
    seen = []
    real = os.replace

    def replace(source, target):
        seen.append((Path(source).read_bytes(), Path(target).name))
        real(source, target)

    monkeypatch.setattr(os, "replace", replace)
    store.backend().put("state/warehouse.duckdb", b"whole file")
    assert seen == [(b"whole file", "warehouse.duckdb")]
    assert store.open("state/warehouse.duckdb").read() == b"whole file"
    assert list(store.backend().folder.rglob("*.tmp")) == []


def test_push_without_a_lease_writes_each_file_through_a_rename(store, tmp_path, monkeypatch):
    replaced = []
    real = os.replace
    monkeypatch.setattr(os, "replace", lambda s, t: (replaced.append(Path(t).name), real(s, t)))
    local = tmp_path / "output"
    local.mkdir()
    (local / "report.csv").write_text("a,b")
    store.push(local, "runs/20260101T000000Z-abc/")
    assert "report.csv" in replaced
    assert store.open("runs/20260101T000000Z-abc/report.csv").read() == b"a,b"


def test_put_if_version_replaces_through_a_rename(store, tmp_path, monkeypatch):
    write(store, "state/a.txt", "v1")
    local = tmp_path / "local"
    lease = store.pull("state/", local)
    (local / "a.txt").write_text("v2")
    replaced = []
    real = os.replace
    monkeypatch.setattr(os, "replace", lambda s, t: (replaced.append(Path(t).name), real(s, t)))
    store.push(local, "state/", lease)
    assert "a.txt" in replaced
    assert store.open("state/a.txt").read() == b"v2"


def test_read_only_without_a_url_map_finds_the_folder_next_to_its_own(project, monkeypatch):
    monkeypatch.delenv(storage.READ_URLS_ENV, raising=False)
    monkeypatch.delenv("PDT_STORAGE_URL", raising=False)
    add_app(project, "warehouse", "schedule: hourly\n")
    add_app(project, "report", "schedule: daily\nstorage:\n  read: [warehouse]\n")
    monkeypatch.chdir(project / "report")
    found = storage.read_only("warehouse")
    assert found.url == (project / ".pdt" / "storage" / "warehouse").as_uri() + "/"
    assert found.read_only
    # A deployed job has its own folder's URL, and the other app's folder is next to it.
    monkeypatch.setenv("PDT_STORAGE_URL", "az://pdt-data/report/")
    assert storage.read_only("warehouse").url == "az://pdt-data/warehouse/"


def test_read_only_uses_the_deployed_urls(project, monkeypatch, tmp_path):
    monkeypatch.setenv(storage.READ_URLS_ENV,
                       json.dumps({"warehouse": tmp_path.as_uri() + "/warehouse/"}))
    assert storage.read_only("warehouse").url == tmp_path.as_uri() + "/warehouse/"
    with pytest.raises(KeyError, match="storage: read:"):
        storage.read_only("other")


def test_fetch_copies_once_and_again_only_after_a_change(store, tmp_path):
    write(store, "state/warehouse.duckdb", "v1")
    local = tmp_path / "cache" / "warehouse.duckdb"
    assert store.fetch("state/warehouse.duckdb", local) == local
    assert local.read_text() == "v1"
    first = local.stat().st_mtime_ns
    local.write_text("v1")  # the same bytes; the version marker says it is current
    assert store.fetch("state/warehouse.duckdb", local) == local
    assert local.read_text() == "v1"
    write(store, "state/warehouse.duckdb", "v2")
    assert store.fetch("state/warehouse.duckdb", local) == local
    assert local.read_text() == "v2"
    assert local.stat().st_mtime_ns >= first
    assert LOCK not in store.ls("state")
    assert list(local.parent.glob("*.tmp")) == []


def test_fetch_of_a_missing_file_returns_none_and_keeps_an_old_copy(store, tmp_path):
    local = tmp_path / "cache" / "warehouse.duckdb"
    assert store.fetch("state/warehouse.duckdb", local) is None
    local.parent.mkdir()
    local.write_text("old")
    assert store.fetch("state/warehouse.duckdb", local) == local
    assert local.read_text() == "old"
