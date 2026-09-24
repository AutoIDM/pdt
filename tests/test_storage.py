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
    file = Path(store.url.removeprefix("file://")) / path
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
    monkeypatch.setenv("PDT_RUN_ID", "run1")
    store.pull("state/", tmp_path / "a")
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


def test_the_cloud_names_the_run(monkeypatch):
    for name in ("CLOUD_RUN_EXECUTION", "CONTAINER_APP_JOB_EXECUTION_NAME",
                 "ECS_CONTAINER_METADATA_URI_V4"):
        monkeypatch.delenv(name, raising=False)
    assert storage.cloud_run_id() == ""
    monkeypatch.setenv("CONTAINER_APP_JOB_EXECUTION_NAME", "pdt-my-report-abc12")
    assert storage.cloud_run_id() == "pdt-my-report-abc12"
    monkeypatch.setenv("CLOUD_RUN_EXECUTION", "pdt-my-report-xyz")
    assert storage.cloud_run_id() == "pdt-my-report-xyz"


def test_an_aws_task_id_comes_from_the_metadata_endpoint(monkeypatch):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps({"TaskARN": "arn:aws:ecs:us-east-1:1:task/pdt/abc123"}).encode()

    asked = []
    monkeypatch.delenv("CLOUD_RUN_EXECUTION", raising=False)
    monkeypatch.delenv("CONTAINER_APP_JOB_EXECUTION_NAME", raising=False)
    monkeypatch.setenv("ECS_CONTAINER_METADATA_URI_V4", "http://169.254.170.2/v4/x")
    monkeypatch.setattr(storage, "urlopen", lambda url, timeout: asked.append(url) or Response())
    assert storage.cloud_run_id() == "abc123"
    assert asked == ["http://169.254.170.2/v4/x/task"]
