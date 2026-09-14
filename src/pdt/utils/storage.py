"""Files that outlive a run: one folder per app in a store pdt created.

Used as a library (`from pdt.utils import storage`).

PDT_STORAGE_URL names the app folder (deploy sets it). Without it the
folder is <project>/.pdt/storage/<app>/, so `pdt run` on a laptop and
the windows provider behave the same as a cloud job.

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
