import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from conftest import add_app
from pdt.config import merged_app, validate_app
from pdt.utils import storage
from pdt.utils.storage import LOCK, Store, StorageConflict, StorageLocked, root


@pytest.fixture
def store(tmp_path):
    folder = tmp_path / "store" / "my-report"
    folder.mkdir(parents=True)
    return Store(folder.as_uri() + "/")


def write(store, path, text):
    file = store.backend().folder / path
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(text)
    return file


def test_pull_then_push_round_trip(store, tmp_path):
    write(store, "state/meltano.db", "v1")
    local = tmp_path / "local"
    lease = store.pull("state/", local)
    assert (local / "meltano.db").read_text() == "v1"
    assert set(lease.versions) == {"state/meltano.db"}
    (local / "meltano.db").write_text("v2")
    (local / "new.txt").write_text("hello")
    store.push(local, "state/", lease)
    assert store.open("state/meltano.db").read() == b"v2"
    assert store.open("state/new.txt").read() == b"hello"
    assert LOCK not in store.ls("state")


def test_a_second_pull_within_the_ttl_is_refused(store, tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "RUN_ID", "run1")
    store.pull("state/", tmp_path / "a")
    monkeypatch.setattr(storage, "RUN_ID", "run2")
    monkeypatch.chdir(store.backend().folder)
    with pytest.raises(StorageLocked) as caught:
        store.pull("state/", tmp_path / "b")
    assert "another run of my-report started at" in str(caught.value)


def test_a_stale_lock_is_taken_over(store, tmp_path, monkeypatch):
    stale = datetime.now(timezone.utc) - timedelta(hours=1)
    write(store, LOCK, json.dumps({"run": "old", "started": stale.isoformat()}))
    monkeypatch.setattr(storage, "RUN_ID", "run2")
    lease = store.pull("state/", tmp_path / "a")
    assert lease.lock["run"] == "run2"
    assert json.loads(store.open(LOCK).read())["run"] == "run2"


def test_push_refuses_a_file_changed_after_the_pull(store, tmp_path):
    write(store, "state/meltano.db", "v1")
    local = tmp_path / "local"
    lease = store.pull("state/", local)
    write(store, "state/meltano.db", "someone else")
    with pytest.raises(StorageConflict) as caught:
        store.push(local, "state/", lease)
    assert "state/meltano.db" in str(caught.value)


def test_push_refuses_a_change_that_kept_the_modification_time(store, tmp_path):
    remote = write(store, "state/meltano.db", "v1")
    stamp = remote.stat().st_mtime_ns
    lease = store.pull("state/", tmp_path / "local")
    remote.write_text("someone else")
    os.utime(remote, ns=(stamp, stamp))
    with pytest.raises(StorageConflict):
        store.push(tmp_path / "local", "state/", lease)


def test_done_is_written_last(store, tmp_path, monkeypatch):
    local = tmp_path / "artifacts"
    local.mkdir()
    (local / "b.csv").write_text("b")
    (local / "a.csv").write_text("a")
    order = []
    monkeypatch.setattr("fsspec.implementations.dirfs.DirFileSystem.pipe_file",
                        lambda self, path, data, **kw: order.append(path))
    store.push(local, "runs/20260101T000000Z-abc/")
    assert order[-1] == "runs/20260101T000000Z-abc/_done"
    assert len(order) == 3


def test_pull_does_not_copy_the_lock(store, tmp_path):
    write(store, "state/x", "x")
    store.pull("state/", tmp_path / "local")
    assert sorted(p.name for p in (tmp_path / "local").iterdir()) == ["x"]


def test_root_falls_back_to_the_project_storage_folder(project, monkeypatch):
    app = add_app(project, "my-report")
    monkeypatch.chdir(app)
    monkeypatch.delenv("PDT_STORAGE_URL", raising=False)
    assert root() == (project / ".pdt" / "storage" / "my-report").as_uri() + "/"
    monkeypatch.setenv("PDT_STORAGE_URL", "s3://pdt-data-abc/my-report/")
    assert root() == "s3://pdt-data-abc/my-report/"


def test_storage_false_opts_out(project):
    add_app(project, "my-report", "storage: false\n")
    assert merged_app("my-report")["storage"] is False
    assert validate_app("my-report") == []


def test_storage_must_be_a_bool(project):
    add_app(project, "my-report", "storage: yes please\n")
    assert any("storage" in problem for problem in validate_app("my-report"))


def test_pull_creates_the_local_folder_on_the_first_run(store, tmp_path):
    lease = store.pull("state/", tmp_path / "fresh")
    assert (tmp_path / "fresh").is_dir()
    assert lease.versions == {}


def test_push_creates_nested_run_folders(store, tmp_path):
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "report.csv").write_text("a\n")
    store.push(tmp_path / "out", "runs/first-abc/artifacts/")
    assert store.ls("runs/first-abc/artifacts/")


def test_held_lock_returns_a_fresh_lock(store):
    started = datetime.now(timezone.utc) - timedelta(minutes=5)
    write(store, LOCK, json.dumps({"run": "abc", "started": started.isoformat()}))
    assert store.held_lock()["run"] == "abc"


def test_held_lock_ignores_a_stale_or_missing_lock(store):
    assert store.held_lock() is None
    stale = datetime.now(timezone.utc) - timedelta(hours=1)
    write(store, LOCK, json.dumps({"run": "old", "started": stale.isoformat()}))
    assert store.held_lock() is None
    write(store, LOCK, "not json")
    assert store.held_lock() is None


def test_usage_counts_files_and_bytes(store):
    assert store.usage() == (0, 0)
    write(store, "runs/a/report.csv", "abc")
    write(store, "state/count.txt", "12")
    assert store.usage() == (2, 5)


def test_a_folder_with_spaces_is_not_percent_encoded(tmp_path):
    # A OneDrive folder such as "OneDrive - Contoso" is a file URI with %20 in it.
    folder = tmp_path / "OneDrive - Contoso" / "project" / "my-report"
    folder.mkdir(parents=True)
    store = Store(folder.as_uri() + "/")
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "report.csv").write_text("a\n")
    store.push(tmp_path / "out", "runs/first-abc/")
    assert (folder / "runs" / "first-abc" / "report.csv").read_text() == "a\n"
    assert store.ls("runs/first-abc") != []
    assert not any("%20" in path.name for path in tmp_path.rglob("*"))


def test_abort_releases_the_lock_without_pushing(store, tmp_path):
    write(store, "state/meltano.db", "v1")
    local = tmp_path / "local"
    lease = store.pull("state/", local)
    (local / "meltano.db").write_text("v2")
    store.abort(lease)
    assert LOCK not in store.ls("state")
    assert store.open("state/meltano.db").read() == b"v1"
    store.pull("state/", tmp_path / "again")


def test_abort_leaves_a_lock_another_run_took_over(store, tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "RUN_ID", "run1")
    lease = store.pull("state/", tmp_path / "a")
    monkeypatch.setattr(storage, "RUN_ID", "run2")
    store.pull("state/", tmp_path / "b", lock_ttl=timedelta(0))
    store.abort(lease)
    assert json.loads(store.open(LOCK).read())["run"] == "run2"


def test_push_leaves_a_lock_another_run_took_over(store, tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "RUN_ID", "run1")
    lease = store.pull("state/", tmp_path / "a")
    monkeypatch.setattr(storage, "RUN_ID", "run2")
    store.pull("state/", tmp_path / "b", lock_ttl=timedelta(0))
    store.push(tmp_path / "a", "state/", lease)
    assert json.loads(store.open(LOCK).read())["run"] == "run2"


def test_a_lock_from_the_same_run_id_is_taken_over_at_once(store, tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "RUN_ID", "retry")
    store.pull("state/", tmp_path / "a")
    lease = store.pull("state/", tmp_path / "b")
    assert lease.lock["run"] == "retry"


def test_a_lock_from_a_dead_process_on_this_machine_is_taken_over(store, tmp_path, monkeypatch):
    fresh = datetime.now(timezone.utc).isoformat()
    write(store, LOCK, json.dumps({"run": "old", "host": storage.HOST, "boot": storage.BOOT_ID,
                                   "pid": 4_000_000, "started": fresh}))
    monkeypatch.setattr(storage, "_process_alive", lambda pid: False)
    monkeypatch.setattr(storage, "RUN_ID", "run2")
    lease = store.pull("state/", tmp_path / "a")
    assert lease.lock["run"] == "run2"


def test_a_lock_from_a_live_process_on_this_machine_is_refused(store, tmp_path, monkeypatch):
    fresh = datetime.now(timezone.utc).isoformat()
    write(store, LOCK, json.dumps({"run": "old", "host": storage.HOST, "boot": storage.BOOT_ID,
                                   "pid": os.getpid(), "started": fresh}))
    monkeypatch.setattr(storage, "RUN_ID", "run2")
    monkeypatch.chdir(store.backend().folder)
    with pytest.raises(StorageLocked) as caught:
        store.pull("state/", tmp_path / "a")
    assert f"on {storage.HOST}" in str(caught.value)
    assert "pdt storage my-report unlock" in str(caught.value)


def test_a_lock_from_another_machine_is_refused_even_when_the_pid_is_dead(
        store, tmp_path, monkeypatch):
    fresh = datetime.now(timezone.utc).isoformat()
    write(store, LOCK, json.dumps({"run": "old", "host": "elsewhere", "boot": "",
                                   "pid": 4_000_000, "started": fresh}))
    monkeypatch.setattr(storage, "_process_alive", lambda pid: False)
    with pytest.raises(StorageLocked):
        store.pull("state/", tmp_path / "a")


def test_a_lock_with_no_machine_details_is_refused_until_the_ttl(store, tmp_path, monkeypatch):
    fresh = datetime.now(timezone.utc).isoformat()
    write(store, LOCK, json.dumps({"run": "old", "started": fresh}))
    monkeypatch.setattr(storage, "RUN_ID", "run2")
    with pytest.raises(StorageLocked):
        store.pull("state/", tmp_path / "a")


def test_process_alive_sees_this_process_and_not_a_missing_one():
    assert storage._process_alive(os.getpid())
    assert not storage._process_alive(4_000_000)


def test_pull_releases_the_lock_when_the_download_fails(store, tmp_path, monkeypatch):
    write(store, "state/x", "x")
    monkeypatch.setattr("fsspec.implementations.dirfs.DirFileSystem.get_file",
                        lambda *a, **kw: (_ for _ in ()).throw(OSError("network")))
    with pytest.raises(OSError):
        store.pull("state/", tmp_path / "a")
    assert LOCK not in store.ls("state")


def test_unlock_removes_any_lock(store):
    store.unlock()
    write(store, LOCK, json.dumps({"run": "old", "started": "2020-01-01T00:00:00+00:00"}))
    store.unlock()
    assert LOCK not in store.ls("state")


def test_sync_pushes_output_and_state_and_releases_the_lock(store, tmp_path, monkeypatch):
    write(store, "state/count.txt", "1")
    monkeypatch.setattr(storage, "RUN_ID", "run1")
    with store.sync(tmp_path / "run") as run:
        assert run.store is store
        assert (run.state / "count.txt").read_text() == "1"
        assert run.output.is_dir()
        assert run.folder.startswith("runs/") and run.folder.endswith("-run1/")
        (run.state / "count.txt").write_text("2")
        (run.output / "report.csv").write_bytes(b"a,b\n")
        assert LOCK in store.ls("state")
    assert store.open("state/count.txt").read() == b"2"
    assert store.open(run.folder + "report.csv").read() == b"a,b\n"
    assert store.open(run.folder + storage.DONE).read() == b""
    assert LOCK not in store.ls("state")


def test_sync_on_error_keeps_state_saves_output_and_releases_the_lock(store, tmp_path):
    write(store, "state/count.txt", "1")
    with pytest.raises(RuntimeError, match="boom"):
        with store.sync(tmp_path / "run") as run:
            (run.state / "count.txt").write_text("2")
            (run.output / "partial.csv").write_bytes(b"a\n")
            raise RuntimeError("boom")
    assert store.open("state/count.txt").read() == b"1"
    assert store.open(run.folder + "partial.csv").read() == b"a\n"
    assert LOCK not in store.ls("state")


def test_sync_releases_the_lock_on_keyboard_interrupt(store, tmp_path):
    with pytest.raises(KeyboardInterrupt):
        with store.sync(tmp_path / "run"):
            raise KeyboardInterrupt
    assert LOCK not in store.ls("state")


def test_sync_with_no_output_creates_no_run_folder(store, tmp_path):
    with store.sync(tmp_path / "run"):
        pass
    assert not store.fs().exists("runs")


def test_sync_defaults_to_a_run_folder_under_dot_pdt(project, monkeypatch):
    app = add_app(project, "my-report")
    monkeypatch.chdir(app)
    monkeypatch.delenv("PDT_STORAGE_URL", raising=False)
    monkeypatch.setattr(storage, "RUN_ID", "run1")
    with storage.sync() as run:
        assert run.state == app / ".pdt" / "runs" / "run1" / "state"
        assert run.store.url == root()
    assert (app / ".pdt" / "runs" / "run1" / "output").is_dir()


def test_sync_can_push_state_on_error(store, tmp_path):
    write(store, "state/token.enc", "old")
    with pytest.raises(RuntimeError):
        with store.sync(tmp_path / "run", push_state_on_error=True) as run:
            (run.state / "token.enc").write_text("rotated")
            raise RuntimeError("load failed")
    assert store.open("state/token.enc").read() == b"rotated"
    assert LOCK not in store.ls("state")


def test_sync_releases_the_lock_when_the_final_push_fails(store, tmp_path):
    write(store, "state/count.txt", "1")
    with pytest.raises(StorageConflict):
        with store.sync(tmp_path / "run") as run:
            write(store, "state/count.txt", "someone else")
            (run.state / "count.txt").write_text("2")
    assert LOCK not in store.ls("state")
    assert store.open("state/count.txt").read() == b"someone else"
