import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from pdt.utils import snowflake_connection
from pdt.utils.storage import LOCK, Store, StorageConflict, StorageLocked, stage_and_key

STAGE = "PDT_DATA.PUBLIC.PDT_DATA"
URL = f"snow://{STAGE}/hello-world/"


@pytest.fixture
def files(monkeypatch):
    held = {"hello-world/state/a.db": b"v1", "hello-world/runs/r1/x.csv": b"a,b\n"}

    def execute(conn, sql, file_stream=None):
        verb, _, rest = sql.partition(" ")
        if verb == "LIST":
            prefix = rest.removeprefix(f"@{STAGE}/")
            return [{"name": f"pdt_data/{key}", "size": len(data), "md5": hashlib.md5(data).hexdigest(),
                     "last_modified": "Tue, 29 Sep 2026 10:00:00 GMT"}
                    for key, data in sorted(held.items()) if key.startswith(prefix)]
        if verb == "GET":
            source, _, target = rest.removeprefix(f"@{STAGE}/").partition(" 'file://")
            if source not in held:
                return []
            (Path(target.removesuffix("/'")) / source.rsplit("/", 1)[-1]).write_bytes(held[source])
            return [{"file": source.rsplit("/", 1)[-1], "size": len(held[source])}]
        if verb == "PUT":
            name = rest.split("'")[1].removeprefix("file://")
            folder = rest.split(f"@{STAGE}/")[1].split(" ")[0]
            key = f"{folder}/{name}"
            if "OVERWRITE = FALSE" in rest and key in held:
                return [{"source": name, "status": "SKIPPED"}]
            held[key] = file_stream.read()
            return [{"source": name, "status": "UPLOADED"}]
        if verb == "REMOVE":
            held.pop(rest.removeprefix(f"@{STAGE}/"), None)
            return []
        raise AssertionError(f"unexpected statement: {sql}")

    monkeypatch.setattr(snowflake_connection, "execute", execute)
    return held


@pytest.fixture
def store(files):
    return Store(URL, object())


def test_a_stage_path_splits_into_the_stage_and_the_file_key():
    assert stage_and_key(f"{STAGE}/a/b") == (STAGE, "a/b")
    assert stage_and_key(STAGE) == (STAGE, "")


def test_ls_lists_the_files_of_a_folder(store):
    assert store.ls("state") == ["state/a.db"]
    assert store.ls("runs") == ["runs/r1"]


def test_ls_of_a_missing_path_is_not_found(store):
    with pytest.raises(FileNotFoundError):
        store.ls("nothing-here")


def test_usage_counts_files_and_bytes(store):
    assert store.usage() == (2, 6)


def test_open_reads_a_staged_file(store):
    assert store.open("state/a.db").read() == b"v1"


def test_a_file_written_through_open_is_staged_and_read_back(store, files):
    with store.open("new.txt", "wb") as f:
        f.write(b"hello")
    assert files["hello-world/new.txt"] == b"hello"
    assert store.open("new.txt").read() == b"hello"


def test_glob_matches_across_folders(store):
    assert store.fs().glob("runs/*/x.csv") == ["runs/r1/x.csv"]


def test_rm_removes_the_named_files(store, files):
    store.fs().rm(["state/a.db", "runs/r1/x.csv"])
    assert files == {}


def test_pull_then_push_round_trip_releases_the_lock(store, files, tmp_path):
    local = tmp_path / "local"
    lease = store.pull("state/", local)
    assert (local / "a.db").read_bytes() == b"v1"
    assert set(lease.versions) == {"state/a.db"}
    assert LOCK in store.ls("state")
    (local / "a.db").write_bytes(b"v2")
    (local / "new.txt").write_bytes(b"hello")
    store.push(local, "state/", lease)
    assert files["hello-world/state/a.db"] == b"v2"
    assert files["hello-world/state/new.txt"] == b"hello"
    assert LOCK not in store.ls("state")


def test_a_second_pull_within_the_ttl_is_refused(store, tmp_path):
    store.pull("state/", tmp_path / "a")
    with pytest.raises(StorageLocked) as caught:
        store.pull("state/", tmp_path / "b")
    assert "another run of hello-world started at" in str(caught.value)


def test_a_stale_lock_is_taken_over(store, files, tmp_path):
    stale = datetime.now(timezone.utc) - timedelta(hours=1)
    files["hello-world/" + LOCK] = json.dumps({"run": "old", "started": stale.isoformat()}).encode()
    lease = store.pull("state/", tmp_path / "a")
    assert lease.lock["run"] != "old"
    assert json.loads(files["hello-world/" + LOCK])["run"] == lease.lock["run"]


def test_push_refuses_a_file_changed_after_the_pull(store, files, tmp_path):
    local = tmp_path / "local"
    lease = store.pull("state/", local)
    files["hello-world/state/a.db"] = b"someone else"
    with pytest.raises(StorageConflict) as caught:
        store.push(local, "state/", lease)
    assert "state/a.db" in str(caught.value)


def test_push_refuses_a_file_created_after_the_pull(store, files, tmp_path):
    local = tmp_path / "local"
    lease = store.pull("state/", local)
    (local / "new.txt").write_bytes(b"mine")
    files["hello-world/state/new.txt"] = b"theirs"
    with pytest.raises(StorageConflict):
        store.push(local, "state/", lease)
    assert files["hello-world/state/new.txt"] == b"theirs"
