"""Files that outlive a run: one folder per app in a store pdt created.

Used as a library (`from pdt.utils import storage`).

PDT_STORAGE_URL names the app folder (every deployed job, including a
Windows scheduled task, gets it set). Without it the folder is
<project>/.pdt/storage/<app>/, so `pdt run` on the user's own computer
behaves the same as a deployed job.

Inside the folder pdt reserves `runs/` and `state/`. `state/lock` is
the lock that pull takes and push or abort releases. A `runs/<time>-<id>/`
folder ends with the run's id: PDT_RUN_ID when set, else the id the cloud
gave the run (the one `pdt runs` lists), else a random one.

    from pdt.utils import storage
    with storage.sync() as run:
        ... read and write files under run.state, write results to run.output ...

sync pulls state/ under the lock, uploads run.output to this run's folder,
and pushes state/ back when the block ends without error. An exception
uploads run.output, releases the lock without touching state/, and
propagates. The same steps by hand:

    s = storage.store()
    lease = s.pull("state/", Path(".pdt-state"))
    try:
        ... work ...
    except BaseException:
        s.abort(lease)
        raise
    s.push(Path(".pdt-state"), "state/", lease)
    s.push(Path("artifacts"), s.run_folder() + "artifacts/")

A run that dies without releasing the lock blocks the next run until the
lock's TTL passes, unless the next run can tell the holder is gone: it
carries the same run id, or it started on this same machine and its
process no longer exists. `pdt storage <app> unlock` releases a lock by hand.
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import sys
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import cached_property
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import url2pathname

from pdt import config

LOCK = "state/lock"
DONE = "_done"
LOCK_TTL = timedelta(minutes=30)


class StorageLocked(Exception):
    pass


class StorageConflict(Exception):
    pass


@dataclass(frozen=True)
class Lease:
    versions: dict[str, str]
    lock: dict


@dataclass(frozen=True)
class Run:
    """What `sync` hands the app: the local folders for this run, the remote
    folder its output lands in, and the store and lease behind them."""

    store: Store
    state: Path
    output: Path
    folder: str
    lease: Lease


def cloud_run_id() -> str:
    """The id the cloud gave this run, so a runs/ folder ends with the id `pdt runs` shows.

    Google Cloud, Azure, and AWS Batch each name the run in an env var.
    """
    for name in ("CLOUD_RUN_EXECUTION", "CONTAINER_APP_JOB_EXECUTION_NAME",
                 "AWS_BATCH_JOB_ID"):
        value = os.environ.get(name, "").strip()
        if value != "":
            return value
    return ""


RUN_ID = (os.environ.get("PDT_RUN_ID", "").strip() or cloud_run_id()
          or uuid.uuid4().hex[:8])
HOST = socket.gethostname()


def _boot_id() -> str:
    """Tells apart two machines that share a hostname, where the OS offers it."""
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return ""


BOOT_ID = _boot_id()


def _process_alive(pid: int) -> bool:
    if sys.platform == "win32":
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.GetLastError() == 5  # ERROR_ACCESS_DENIED: exists, not ours
        code = ctypes.c_ulong()
        try:
            return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def reclaimable(held: dict) -> bool:
    """Whether the run that wrote this lock body is known to be gone: it is
    this same run started again, or it ran on this machine and its process
    has ended."""
    if held.get("run") == RUN_ID:
        return True
    same_machine = held.get("host") == HOST and held.get("boot", "") == BOOT_ID
    pid = held.get("pid")
    return same_machine and isinstance(pid, int) and not _process_alive(pid)


def root() -> str:
    url = os.environ.get("PDT_STORAGE_URL", "").strip()
    if url != "":
        return url
    folder = config.find_project() / ".pdt" / "storage" / config.running_app_dir().name
    return folder.as_uri() + "/"


def store(credentials=None) -> Store:
    return Store(root(), credentials)


def sync(local: Path | None = None, lock_ttl: timedelta = LOCK_TTL,
         push_state_on_error: bool = False):
    """`store().sync(...)`: the one call most apps need. See Store.sync."""
    return store().sync(local, lock_ttl, push_state_on_error)


class Backend:
    """Native calls of one URL scheme. The url is the app folder with a
    trailing slash; every path is relative to it."""

    def __init__(self, url: str, credentials):
        self.url = url
        self.credentials = credentials
        self.host, self.prefix = url.split("://", 1)[1].split("/", 1)

    def key(self, path: str) -> str:
        return self.prefix + path

    def filesystem(self):
        raise NotImplementedError

    def dirfs(self):
        """An fsspec filesystem whose paths are relative to the app folder."""
        from fsspec.implementations.dirfs import DirFileSystem
        fs = self.filesystem()
        return DirFileSystem(path=fs._strip_protocol(self.url).rstrip("/"), fs=fs)

    def create_if_absent(self, path: str, data: bytes) -> bool:
        try:
            self.put_if_version(path, data, None)
        except StorageConflict:
            return False
        return True

    def versions(self, prefix: str) -> dict[str, str]:
        raise NotImplementedError

    def put_if_version(self, path: str, data: bytes, version: str | None) -> None:
        raise NotImplementedError

    def delete(self, path: str) -> None:
        raise NotImplementedError


class Local(Backend):
    def __init__(self, url: str, credentials):
        super().__init__(url, credentials)
        self.folder = Path(url2pathname(urlparse(url).path))

    def filesystem(self):
        import fsspec
        return fsspec.filesystem("file", auto_mkdir=True)

    def dirfs(self):
        # fsspec keeps %20 in a file URI's path, so a space in a folder name
        # would become a new folder named with %20. Use the decoded path.
        from fsspec.implementations.dirfs import DirFileSystem
        return DirFileSystem(path=str(self.folder), fs=self.filesystem())

    def file(self, path: str) -> Path:
        return self.folder / path

    def create_if_absent(self, path: str, data: bytes) -> bool:
        self.file(path).parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self.file(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return False
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        return True

    def versions(self, prefix: str) -> dict[str, str]:
        out = {}
        for file in self.folder.rglob("*"):
            rel = file.relative_to(self.folder).as_posix()
            if file.is_file() and rel.startswith(prefix):
                out[rel] = hashlib.sha256(file.read_bytes()).hexdigest()
        return out

    def put_if_version(self, path: str, data: bytes, version: str | None) -> None:
        if version is None:
            if not self.create_if_absent(path, data):
                raise StorageConflict(f"{path} was created by another run")
            return
        file = self.file(path)
        if not file.is_file() or hashlib.sha256(file.read_bytes()).hexdigest() != version:
            raise StorageConflict(f"{path} changed since it was pulled")
        self.file(path).write_bytes(data)

    def delete(self, path: str) -> None:
        self.file(path).unlink(missing_ok=True)


class S3(Backend):
    @cached_property
    def client(self):
        import boto3
        return (self.credentials or boto3.Session()).client("s3")

    def filesystem(self):
        import fsspec
        options = {}
        if self.credentials is not None:
            creds = self.credentials.get_credentials().get_frozen_credentials()
            options = {"key": creds.access_key, "secret": creds.secret_key, "token": creds.token}
        return fsspec.filesystem("s3", **options)

    def versions(self, prefix: str) -> dict[str, str]:
        out = {}
        pages = self.client.get_paginator("list_objects_v2").paginate(
            Bucket=self.host, Prefix=self.key(prefix))
        for page in pages:
            for obj in page.get("Contents", []):
                out[obj["Key"][len(self.prefix):]] = obj["ETag"]
        return out

    def put_if_version(self, path: str, data: bytes, version: str | None) -> None:
        from botocore.exceptions import ClientError
        condition = {"IfNoneMatch": "*"} if version is None else {"IfMatch": version}
        try:
            self.client.put_object(Bucket=self.host, Key=self.key(path), Body=data, **condition)
        except ClientError as e:
            if e.response["Error"]["Code"] in ("PreconditionFailed", "ConditionalRequestConflict"):
                raise StorageConflict(f"{path} changed since it was pulled")
            raise

    def delete(self, path: str) -> None:
        self.client.delete_object(Bucket=self.host, Key=self.key(path))


class GCS(Backend):
    @cached_property
    def bucket(self):
        from google.cloud import storage
        return storage.Client(credentials=self.credentials).bucket(self.host)

    def filesystem(self):
        import fsspec
        return fsspec.filesystem("gs", token=self.credentials)

    def versions(self, prefix: str) -> dict[str, str]:
        blobs = self.bucket.list_blobs(prefix=self.key(prefix))
        return {blob.name[len(self.prefix):]: str(blob.generation) for blob in blobs}

    def put_if_version(self, path: str, data: bytes, version: str | None) -> None:
        from google.api_core.exceptions import PreconditionFailed
        generation = 0 if version is None else int(version)
        try:
            self.bucket.blob(self.key(path)).upload_from_string(
                data, if_generation_match=generation)
        except PreconditionFailed:
            raise StorageConflict(f"{path} changed since it was pulled")

    def delete(self, path: str) -> None:
        self.bucket.blob(self.key(path)).delete()


class Azure(Backend):
    def __init__(self, url: str, credentials):
        super().__init__(url, credentials)
        self.container, self.account_host = self.host.split("@", 1)

    @cached_property
    def credential(self):
        if self.credentials is not None:
            return self.credentials
        from azure.identity import DefaultAzureCredential
        return DefaultAzureCredential()

    @cached_property
    def client(self):
        from azure.storage.blob import BlobServiceClient
        account_url = "https://" + self.account_host.replace(".dfs.", ".blob.", 1)
        return BlobServiceClient(account_url, self.credential).get_container_client(self.container)

    def filesystem(self):
        import fsspec
        return fsspec.filesystem("abfs", account_name=self.account_host.split(".", 1)[0],
                                 credential=self.credential)

    def versions(self, prefix: str) -> dict[str, str]:
        blobs = self.client.list_blobs(name_starts_with=self.key(prefix))
        return {blob.name[len(self.prefix):]: blob.etag for blob in blobs}

    def put_if_version(self, path: str, data: bytes, version: str | None) -> None:
        from azure.core import MatchConditions
        from azure.core.exceptions import ResourceExistsError, ResourceModifiedError
        if version is None:
            condition = {"overwrite": False}
        else:
            condition = {"overwrite": True, "etag": version,
                         "match_condition": MatchConditions.IfNotModified}
        try:
            self.client.upload_blob(self.key(path), data, **condition)
        except (ResourceExistsError, ResourceModifiedError):
            raise StorageConflict(f"{path} changed since it was pulled")

    def delete(self, path: str) -> None:
        self.client.delete_blob(self.key(path))


BACKENDS: dict[str, type[Backend]] = {
    "file": Local,
    "s3": S3,
    "gs": GCS,
    "abfs": Azure,
}


@dataclass(frozen=True)
class Store:
    url: str
    credentials: object | None = None

    def backend(self) -> Backend:
        scheme = self.url.split("://", 1)[0]
        return BACKENDS[scheme](self.url, self.credentials)

    def fs(self):
        return self.backend().dirfs()

    def open(self, path: str, mode: str = "rb"):
        return self.fs().open(path, mode)

    def ls(self, path: str = "") -> list[str]:
        return self.fs().ls(path, detail=False)

    def run_folder(self) -> str:
        return f"runs/{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{RUN_ID}/"

    def usage(self) -> tuple[int, int]:
        """Object count and total bytes under the app folder."""
        files = [entry for entry in self.fs().find("", detail=True).values()
                 if entry.get("type") == "file"]
        return len(files), sum(entry.get("size") or 0 for entry in files)

    def read_lock(self) -> dict | None:
        """The lock body as written, however old, or None when there is none."""
        try:
            with self.open(LOCK) as f:
                held = json.loads(f.read())
            datetime.fromisoformat(held["started"])
        except (OSError, ValueError, KeyError, TypeError):
            return None
        return held

    def held_lock(self, ttl: timedelta = LOCK_TTL) -> dict | None:
        """The lock body while a run younger than ttl still holds the state."""
        held = self.read_lock()
        if held is None:
            return None
        if datetime.now(timezone.utc) - datetime.fromisoformat(held["started"]) >= ttl:
            return None
        return held

    def unlock(self) -> None:
        """Release the state lock whoever holds it. For a person who knows the
        run is dead; a run releases its own lock with push or abort."""
        if self.read_lock() is not None:
            self.backend().delete(LOCK)

    def pull(self, remote: str, local: Path, lock_ttl: timedelta = LOCK_TTL) -> Lease:
        backend = self.backend()
        lock = self._take_lock(backend, lock_ttl) if remote.startswith("state") else {}
        try:
            versions = backend.versions(remote)
            versions.pop(LOCK, None)
            local.mkdir(parents=True, exist_ok=True)
            fs = backend.dirfs()
            for path in versions:
                target = local / path[len(remote):]
                target.parent.mkdir(parents=True, exist_ok=True)
                fs.get_file(path, str(target))
        except BaseException:
            self._release_lock(backend, Lease({}, lock))
            raise
        return Lease(versions, lock)

    def push(self, local: Path, remote: str, lease: Lease | None = None) -> None:
        backend = self.backend()
        fs = backend.dirfs()
        files = [(remote + file.relative_to(local).as_posix(), file.read_bytes())
                 for file in sorted(local.rglob("*")) if file.is_file()]
        if remote.startswith("runs/"):
            files.append((remote + DONE, b""))
        for path, data in files:
            if lease is None:
                fs.pipe_file(path, data)
            else:
                backend.put_if_version(path, data, lease.versions.get(path))
        if lease is not None:
            self._release_lock(backend, lease)

    def abort(self, lease: Lease) -> None:
        """Give up a pull without pushing: release the lock and leave the
        remote files as they were."""
        self._release_lock(self.backend(), lease)

    @contextmanager
    def sync(self, local: Path | None = None, lock_ttl: timedelta = LOCK_TTL,
             push_state_on_error: bool = False):
        """Pull state/ into local/state under the lock and yield a Run.

        When the block ends, files under local/output go to this run's
        folder, then state/ goes back and the lock is released. When the
        block raises, output still goes up, the lock is released, and the
        exception propagates; state/ is left as it was, so the next run
        starts from the last good state. An app whose state is safe to
        keep at any point (a Meltano bookmark, a rotated OAuth token) sets
        push_state_on_error so a failed run keeps what it saved. local
        defaults to .pdt/runs/<run id>/ in the app folder, and is kept.
        """
        local = (Path(local) if local is not None
                 else config.running_app_dir() / ".pdt" / "runs" / RUN_ID)
        state, output = local / "state", local / "output"
        lease = self.pull("state/", state, lock_ttl)
        run = Run(self, state, output, self.run_folder(), lease)
        output.mkdir(parents=True, exist_ok=True)
        try:
            yield run
        except BaseException:
            self._end(run, push_state_on_error)
            raise
        self._end(run, True)

    def _end(self, run: Run, push_state: bool) -> None:
        """Upload the run's output, then push state/ or abort. The lock is
        released on every path, and a failed upload propagates."""
        try:
            if any(file.is_file() for file in run.output.rglob("*")):
                self.push(run.output, run.folder)
            if push_state:
                self.push(run.state, "state/", run.lease)
        except BaseException:
            self.abort(run.lease)
            raise
        if not push_state:
            self.abort(run.lease)

    def _take_lock(self, backend: Backend, ttl: timedelta) -> dict:
        lock = {"run": RUN_ID, "host": HOST, "boot": BOOT_ID, "pid": os.getpid(),
                "started": datetime.now(timezone.utc).isoformat()}
        data = json.dumps(lock).encode()
        if backend.create_if_absent(LOCK, data):
            return lock
        held = self.held_lock(ttl)
        if held is not None and not reclaimable(held):
            # The URL does not always end in the app name (Windows adds storage/).
            app = config.running_app_dir().name
            where = f" on {held['host']}" if held.get("host") else ""
            raise StorageLocked(
                f"another run of {app} started at {held['started']}{where} still holds "
                f"the state; if that run is dead, release it with: pdt storage {app} unlock")
        backend.put_if_version(LOCK, data, backend.versions(LOCK)[LOCK])
        return lock

    def _release_lock(self, backend: Backend, lease: Lease) -> None:
        """Delete the lock only while it is still the one this lease took. A
        lock another run took over after the TTL passed stays with that run."""
        if not lease.lock:
            return
        held = self.read_lock()
        if held is not None and (held.get("run"), held.get("started")) == (
                lease.lock["run"], lease.lock["started"]):
            backend.delete(LOCK)
