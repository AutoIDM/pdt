from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path


class StateConflict(Exception):
    """Another run changed the state too many times."""


_EMPTY = {"format": 1, "values": {}}
_RETRIES = 5


def _document(raw) -> dict:
    if not isinstance(raw, dict) or set(raw) != {"format", "values"}:
        raise ValueError("state document must contain only format and values")
    if raw["format"] != 1:
        raise ValueError("state document format must be 1")
    if not isinstance(raw["values"], dict):
        raise ValueError("state document values must be an object")
    try:
        json.dumps(raw["values"])
    except (TypeError, ValueError) as exc:
        raise ValueError("state document values must be JSON") from exc
    return raw


def _location() -> str:
    value = os.environ.get("PDT_STATE_LOCATION", "").strip()
    if value:
        return value
    from pdt import config
    try:
        project = config.find_project()
    except config.ConfigError:
        project = Path.cwd()
    return f"sqlite:///{project / '.pdt' / 'state.sqlite3'}"


def _app_name() -> str:
    name = os.environ.get("PDT_STATE_APP", "").strip()
    if name:
        return name
    return Path.cwd().name


def _sqlite_path(location: str) -> Path:
    if not location.startswith("sqlite:///"):
        raise ValueError(f"unsupported PDT_STATE_LOCATION: {location}")
    return Path(location.removeprefix("sqlite:///"))


class _SQLiteBackend:
    def __init__(self, path: Path, app_name: str):
        self.path = path
        self.app_name = app_name

    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS state "
            "(app TEXT PRIMARY KEY, document TEXT NOT NULL, revision INTEGER NOT NULL)")
        return connection

    def read(self) -> tuple[dict, int | None]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT document, revision FROM state WHERE app = ?", (self.app_name,)).fetchone()
        if row is None:
            return dict(_EMPTY), None
        return _document(json.loads(row[0])), row[1]

    def write(self, document: dict, revision: int | None) -> bool:
        payload = json.dumps(document, sort_keys=True, separators=(",", ":"))
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if revision is None:
                cursor = connection.execute(
                    "INSERT OR IGNORE INTO state(app, document, revision) VALUES (?, ?, 1)",
                    (self.app_name, payload))
            else:
                cursor = connection.execute(
                    "UPDATE state SET document = ?, revision = revision + 1 "
                    "WHERE app = ? AND revision = ?",
                    (payload, self.app_name, revision))
            connection.commit()
        return cursor.rowcount == 1


def _local_state_exists(project: Path, app_name: str) -> bool:
    path = project / ".pdt" / "state.sqlite3"
    if not path.is_file():
        return False
    try:
        with sqlite3.connect(path) as connection:
            row = connection.execute(
                "SELECT 1 FROM state WHERE app = ?", (app_name,)).fetchone()
    except sqlite3.OperationalError:
        return False
    return row is not None


def _delete_local_state(project: Path, app_name: str) -> bool:
    path = project / ".pdt" / "state.sqlite3"
    if not path.is_file():
        return False
    try:
        with sqlite3.connect(path) as connection:
            cursor = connection.execute("DELETE FROM state WHERE app = ?", (app_name,))
            remaining = connection.execute("SELECT 1 FROM state LIMIT 1").fetchone()
    except sqlite3.OperationalError:
        return False
    if cursor.rowcount and remaining is None:
        path.unlink(missing_ok=True)
        Path(f"{path}-wal").unlink(missing_ok=True)
        Path(f"{path}-shm").unlink(missing_ok=True)
        try:
            path.parent.rmdir()
        except OSError:
            pass
    return cursor.rowcount == 1


class _S3Backend:
    def __init__(self, location: str):
        bucket, key = location.removeprefix("s3://").split("/", 1)
        import boto3
        self.client = boto3.client("s3")
        self.bucket = bucket
        self.key = key

    def read(self):
        from botocore.exceptions import ClientError
        try:
            result = self.client.get_object(Bucket=self.bucket, Key=self.key)
        except ClientError as exc:
            if exc.response["Error"].get("Code") in {"NoSuchKey", "NoSuchBucket", "404"}:
                return dict(_EMPTY), None
            raise
        return _document(json.loads(result["Body"].read())), result["ETag"]

    def write(self, document, revision):
        from botocore.exceptions import ClientError
        request = {"Bucket": self.bucket, "Key": self.key,
                   "Body": json.dumps(document, sort_keys=True), "ContentType": "application/json"}
        condition = "IfNoneMatch" if revision is None else "IfMatch"
        request[condition] = "*" if revision is None else revision
        try:
            self.client.put_object(**request)
            return True
        except ClientError as exc:
            if exc.response["Error"].get("Code") in {"PreconditionFailed", "412"}:
                return False
            raise


class _GoogleCloudBackend:
    def __init__(self, location: str):
        bucket, key = location.removeprefix("gs://").split("/", 1)
        from google.cloud import storage
        self.bucket = storage.Client().bucket(bucket)
        self.key = key

    def read(self):
        from google.api_core.exceptions import NotFound, PreconditionFailed
        for _attempt in range(_RETRIES):
            blob = self.bucket.get_blob(self.key)
            if blob is None:
                return dict(_EMPTY), None
            try:
                payload = blob.download_as_bytes(if_generation_match=blob.generation)
                return _document(json.loads(payload)), blob.generation
            except NotFound:
                return dict(_EMPTY), None
            except PreconditionFailed:
                continue
        raise StateConflict("another run kept changing this app's state")

    def write(self, document, revision):
        from google.api_core.exceptions import PreconditionFailed
        blob = self.bucket.blob(self.key)
        try:
            blob.upload_from_string(
                json.dumps(document, sort_keys=True), content_type="application/json",
                if_generation_match=0 if revision is None else revision)
            return True
        except PreconditionFailed:
            return False


class _AzureBlobBackend:
    def __init__(self, location: str):
        account, container, blob = location.removeprefix("azblob://").split("/", 2)
        from azure.identity import DefaultAzureCredential
        from azure.storage.blob import BlobClient
        self.blob = BlobClient(
            f"https://{account}.blob.core.windows.net", container, blob,
            credential=DefaultAzureCredential())

    def read(self):
        from azure.core.exceptions import ResourceNotFoundError
        try:
            result = self.blob.download_blob()
            payload = result.readall()
            return _document(json.loads(payload)), result.properties.etag
        except ResourceNotFoundError:
            return dict(_EMPTY), None

    def write(self, document, revision):
        from azure.core import MatchConditions
        from azure.core.exceptions import ResourceExistsError, ResourceModifiedError
        try:
            self.blob.upload_blob(
                json.dumps(document, sort_keys=True), overwrite=True,
                if_none_match="*" if revision is None else None,
                etag=revision, match_condition=MatchConditions.IfNotModified
                if revision is not None else None)
            return True
        except (ResourceExistsError, ResourceModifiedError):
            return False


def _backend():
    location = _location()
    if location.startswith("sqlite:///"):
        return _SQLiteBackend(_sqlite_path(location), _app_name())
    if location.startswith("s3://"):
        return _S3Backend(location)
    if location.startswith("gs://"):
        return _GoogleCloudBackend(location)
    if location.startswith("azblob://"):
        return _AzureBlobBackend(location)
    raise ValueError(f"unsupported PDT_STATE_LOCATION: {location}")


def read() -> dict:
    """Return the current app state values."""
    document, _revision = _backend().read()
    return json.loads(json.dumps(document["values"]))


def update(change) -> dict:
    """Apply change to the newest state and return its values."""
    backend = _backend()
    for _attempt in range(_RETRIES):
        document, revision = backend.read()
        values = change(json.loads(json.dumps(document["values"])))
        _document({"format": 1, "values": values})
        updated = {"format": 1, "values": values}
        if backend.write(updated, revision):
            return json.loads(json.dumps(values))
    raise StateConflict("another run kept changing this app's state")
