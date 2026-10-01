"""Files that outlive a run: one folder per app in a store pdt created.

Used as a library (`from pdt.utils import storage`).

PDT_STORAGE_URL names the app folder (every deployed job, including a
Windows scheduled task, gets it set). Without it the folder is
<project>/.pdt/storage/<app>/, so `pdt run` on the user's own computer
behaves the same as a deployed job.

Inside the folder pdt reserves `runs/` and `state/`. `state/lock` is
the lock that pull takes and push releases.

    from pdt.utils import storage
    s = storage.store()
    lease = s.pull("state/", Path(".pdt-state"))
    ... work ...
    s.push(Path(".pdt-state"), "state/", lease)
    s.push(Path("artifacts"), s.run_folder() + "artifacts/")
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
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


RUN_ID = os.environ.get("PDT_RUN_ID", "").strip() or uuid.uuid4().hex[:8]


def root() -> str:
    url = os.environ.get("PDT_STORAGE_URL", "").strip()
    if url != "":
        return url
    folder = config.find_project() / ".pdt" / "storage" / Path.cwd().name
    return folder.as_uri() + "/"


def store(credentials=None) -> Store:
    return Store(root(), credentials)


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


def stage_and_key(path: str) -> tuple[str, str]:
    """`<database>.<schema>.<stage>/<key>` split at the first slash."""
    stage, _, key = path.strip("/").partition("/")
    return stage, key


class StageFileSystem:
    """The fsspec filesystem methods for Snowflake internal stages.

    A path is `<database>.<schema>.<stage>/<file path>`. A stage has no
    folders, so a folder is the set of files whose path starts with it.
    `stage_filesystem` joins this with fsspec's AbstractFileSystem, which
    supplies find, glob, walk, get, and open on top of these.
    """

    protocol = "snow"

    def __init__(self, conn, **kwargs):
        super().__init__(**kwargs)
        self.conn = conn

    @classmethod
    def _strip_protocol(cls, path):
        if isinstance(path, list):
            return [cls._strip_protocol(item) for item in path]
        return str(path).removeprefix("snow://").strip("/")

    def _files(self, stage: str, prefix: str) -> list[dict]:
        from pdt.utils.snowflake_connection import execute
        rows = execute(self.conn, f"LIST @{stage}/{prefix}")
        found = []
        for row in rows:
            # LIST prefixes every name with the stage's own name.
            key = str(row["name"]).partition("/")[2]
            if not key.startswith(prefix):
                continue
            found.append({"name": f"{stage}/{key}", "size": int(row["size"] or 0),
                          "type": "file", "md5": row.get("md5"),
                          "last_modified": row.get("last_modified")})
        return found

    def _file(self, path: str) -> dict | None:
        stage, key = stage_and_key(path)
        return next((entry for entry in self._files(stage, key)
                     if entry["name"] == f"{stage}/{key}"), None)

    def ls(self, path, detail=False, **kwargs):
        path = self._strip_protocol(path)
        stage, key = stage_and_key(path)
        prefix = key + "/" if key else ""
        entries: dict[str, dict] = {}
        for entry in self._files(stage, prefix):
            rest = entry["name"][len(stage) + 1 + len(prefix):]
            if "/" in rest:
                folder = f"{stage}/{prefix}{rest.split('/', 1)[0]}"
                entries.setdefault(folder, {"name": folder, "size": 0, "type": "directory"})
            else:
                entries[entry["name"]] = entry
        if not entries and key:
            found = self._file(path)
            if found is None:
                raise FileNotFoundError(path)
            entries[found["name"]] = found
        listed = sorted(entries.values(), key=lambda entry: entry["name"])
        return listed if detail else [entry["name"] for entry in listed]

    def info(self, path, **kwargs):
        path = self._strip_protocol(path)
        stage, key = stage_and_key(path)
        found = self._file(path) if key else None
        if found is not None:
            return found
        if not key or self._files(stage, key + "/"):
            return {"name": path, "size": 0, "type": "directory"}
        raise FileNotFoundError(path)

    def get_file(self, rpath, lpath, **kwargs):
        import shutil
        import tempfile
        from pdt.utils.snowflake_connection import execute
        stage, key = stage_and_key(self._strip_protocol(rpath))
        with tempfile.TemporaryDirectory(prefix="pdt-stage-") as tmp:
            rows = execute(self.conn, f"GET @{stage}/{key} 'file://{Path(tmp).as_posix()}/'")
            if not rows:
                raise FileNotFoundError(rpath)
            shutil.move(Path(tmp) / key.rsplit("/", 1)[-1], lpath)

    def cat_file(self, path, start=None, end=None, **kwargs):
        import tempfile
        with tempfile.TemporaryDirectory(prefix="pdt-stage-") as tmp:
            target = Path(tmp) / "file"
            self.get_file(path, target)
            return target.read_bytes()[start:end]

    def _put(self, path: str, data: bytes, overwrite: bool) -> str:
        import io
        from pdt.utils.snowflake_connection import execute
        stage, key = stage_and_key(self._strip_protocol(path))
        folder, _, name = key.rpartition("/")
        rows = execute(
            self.conn,
            f"PUT 'file://{name}' @{stage}/{folder} AUTO_COMPRESS = FALSE "
            f"OVERWRITE = {'TRUE' if overwrite else 'FALSE'}",
            file_stream=io.BytesIO(data))
        return str(rows[0]["status"]).upper() if rows else ""

    def pipe_file(self, path, value, **kwargs):
        self._put(path, value, overwrite=True)

    def put_file(self, lpath, rpath, **kwargs):
        self._put(rpath, Path(lpath).read_bytes(), overwrite=True)

    def rm_file(self, path):
        from pdt.utils.snowflake_connection import execute
        stage, key = stage_and_key(self._strip_protocol(path))
        execute(self.conn, f"REMOVE @{stage}/{key}")

    def rm(self, path, recursive=False, maxdepth=None):
        for item in self._strip_protocol(path if isinstance(path, list) else [path]):
            if self.info(item)["type"] == "directory":
                for entry in self.find(item):
                    self.rm_file(entry)
            else:
                self.rm_file(item)

    def _open(self, path, mode="rb", **kwargs):
        import io
        if "r" in mode:
            return io.BytesIO(self.cat_file(path))
        fs = self

        class Writer(io.BytesIO):
            def close(self):
                if not self.closed:
                    fs.pipe_file(path, self.getvalue())
                super().close()

        return Writer()


_STAGE_FILESYSTEM: type | None = None


def stage_filesystem(conn):
    global _STAGE_FILESYSTEM
    if _STAGE_FILESYSTEM is None:
        from fsspec import AbstractFileSystem
        _STAGE_FILESYSTEM = type("StageFileSystem", (StageFileSystem, AbstractFileSystem), {})
    return _STAGE_FILESYSTEM(conn)


class Snowflake(Backend):
    """snow://<database>.<schema>.<stage>/<app>/, an internal stage.

    `credentials` is an open connection. Inside a job it is None, and the
    job's own session token opens one. A stage has no conditional write,
    so a version check reads the file's md5 first and writes after it.
    """

    @cached_property
    def conn(self):
        if self.credentials is not None:
            return self.credentials
        from pdt.utils import snowflake_connection
        return snowflake_connection.connect()

    def filesystem(self):
        return stage_filesystem(self.conn)

    def versions(self, prefix: str) -> dict[str, str]:
        fs = self.filesystem()
        return {entry["name"][len(self.host) + 1 + len(self.prefix):]: str(entry["md5"])
                for entry in fs._files(self.host, self.key(prefix))}

    def put_if_version(self, path: str, data: bytes, version: str | None) -> None:
        fs = self.filesystem()
        full = f"{self.host}/{self.key(path)}"
        current = fs._file(full)
        if version is None:
            if current is not None or fs._put(full, data, overwrite=False) != "UPLOADED":
                raise StorageConflict(f"{path} was created by another run")
            return
        if current is None or str(current["md5"]) != version:
            raise StorageConflict(f"{path} changed since it was pulled")
        fs._put(full, data, overwrite=True)

    def delete(self, path: str) -> None:
        self.filesystem().rm_file(f"{self.host}/{self.key(path)}")


BACKENDS: dict[str, type[Backend]] = {
    "file": Local,
    "s3": S3,
    "gs": GCS,
    "abfs": Azure,
    "snow": Snowflake,
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

    def held_lock(self, ttl: timedelta = LOCK_TTL) -> dict | None:
        """The lock body while a run younger than ttl still holds the state."""
        try:
            with self.open(LOCK) as f:
                held = json.loads(f.read())
            started = datetime.fromisoformat(held["started"])
        except (OSError, ValueError, KeyError, TypeError):
            return None
        if datetime.now(timezone.utc) - started >= ttl:
            return None
        return held

    def pull(self, remote: str, local: Path, lock_ttl: timedelta = LOCK_TTL) -> Lease:
        backend = self.backend()
        lock = self._take_lock(backend, lock_ttl) if remote.startswith("state") else {}
        versions = backend.versions(remote)
        versions.pop(LOCK, None)
        local.mkdir(parents=True, exist_ok=True)
        fs = backend.dirfs()
        for path in versions:
            target = local / path[len(remote):]
            target.parent.mkdir(parents=True, exist_ok=True)
            fs.get_file(path, str(target))
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
        if lease is not None and lease.lock:
            backend.delete(LOCK)

    def _take_lock(self, backend: Backend, ttl: timedelta) -> dict:
        lock = {"run": RUN_ID, "started": datetime.now(timezone.utc).isoformat()}
        data = json.dumps(lock).encode()
        if backend.create_if_absent(LOCK, data):
            return lock
        held = self.held_lock(ttl)
        if held is not None:
            app = self.url.rstrip("/").rsplit("/", 1)[-1]
            raise StorageLocked(
                f"another run of {app} started at {held['started']} still holds the state")
        backend.put_if_version(LOCK, data, backend.versions(LOCK)[LOCK])
        return lock
