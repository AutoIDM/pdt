from __future__ import annotations

import json
import sys
import types

import pytest

from pdt.utils import state


def setup_state(monkeypatch, tmp_path):
    monkeypatch.setenv("PDT_STATE_LOCATION", f"sqlite:///{tmp_path / 'state.sqlite3'}")
    monkeypatch.setenv("PDT_STATE_APP", "report")


def test_read_and_update(monkeypatch, tmp_path):
    setup_state(monkeypatch, tmp_path)
    assert state.read() == {}
    assert state.update(lambda values: {**values, "last_successful_run": "now"}) == {
        "last_successful_run": "now"}
    assert state.read() == {"last_successful_run": "now"}


def test_local_state_is_separate_per_app(monkeypatch, tmp_path):
    setup_state(monkeypatch, tmp_path)
    state.update(lambda _values: {"app": "report"})
    monkeypatch.setenv("PDT_STATE_APP", "audit")
    assert state.read() == {}


def test_local_state_delete_preserves_other_apps(monkeypatch, tmp_path):
    project = tmp_path
    monkeypatch.setenv(
        "PDT_STATE_LOCATION", f"sqlite:///{project / '.pdt' / 'state.sqlite3'}")
    monkeypatch.setenv("PDT_STATE_APP", "report")
    state.update(lambda _values: {"app": "report"})
    monkeypatch.setenv("PDT_STATE_APP", "audit")
    state.update(lambda _values: {"app": "audit"})
    assert state._delete_local_state(project, "report")
    assert state.read() == {"app": "audit"}
    assert state._delete_local_state(project, "audit")
    assert not (project / ".pdt" / "state.sqlite3").exists()


def test_update_rejects_bad_values(monkeypatch, tmp_path):
    setup_state(monkeypatch, tmp_path)
    with pytest.raises(ValueError, match="values must be an object"):
        state.update(lambda _values: [])


def test_document_rejects_unknown_fields():
    with pytest.raises(ValueError, match="only format and values"):
        state._document({"format": 1, "values": {}, "revision": 1})


def test_update_retries_a_conflict(monkeypatch):
    class Backend:
        def __init__(self):
            self.writes = 0

        def read(self):
            return {"format": 1, "values": {"count": self.writes}}, self.writes

        def write(self, _document, _revision):
            self.writes += 1
            return self.writes == 2

    backend = Backend()
    monkeypatch.setattr(state, "_backend", lambda: backend)
    assert state.update(lambda values: {"count": values["count"] + 1}) == {"count": 2}


def test_update_reports_final_conflict(monkeypatch):
    class Backend:
        def read(self):
            return {"format": 1, "values": {}}, None

        def write(self, _document, _revision):
            return False

    monkeypatch.setattr(state, "_backend", Backend)
    with pytest.raises(state.StateConflict):
        state.update(lambda values: values)


def test_s3_conditional_write(monkeypatch):
    exceptions = types.ModuleType("botocore.exceptions")
    exceptions.ClientError = Exception
    monkeypatch.setitem(sys.modules, "botocore", types.ModuleType("botocore"))
    monkeypatch.setitem(sys.modules, "botocore.exceptions", exceptions)

    class Client:
        def put_object(self, **request):
            self.request = request

    backend = state._S3Backend.__new__(state._S3Backend)
    backend.client = Client()
    backend.bucket = "bucket"
    backend.key = "apps/report.json"
    assert backend.write({"format": 1, "values": {}}, '"etag"')
    assert backend.client.request["IfMatch"] == '"etag"'


def test_google_conditional_write(monkeypatch):
    exceptions = types.ModuleType("google.api_core.exceptions")
    exceptions.PreconditionFailed = Exception
    monkeypatch.setitem(sys.modules, "google", types.ModuleType("google"))
    monkeypatch.setitem(sys.modules, "google.api_core", types.ModuleType("google.api_core"))
    monkeypatch.setitem(sys.modules, "google.api_core.exceptions", exceptions)

    class Blob:
        def upload_from_string(self, payload, **request):
            self.payload = json.loads(payload)
            self.request = request

    backend = state._GoogleCloudBackend.__new__(state._GoogleCloudBackend)
    blob = Blob()
    backend.bucket = types.SimpleNamespace(blob=lambda _key: blob)
    backend.key = "apps/report.json"
    assert backend.write({"format": 1, "values": {}}, 4)
    assert blob.request["if_generation_match"] == 4


def test_google_read_pins_the_download_generation(monkeypatch):
    class NotFound(Exception):
        pass

    class PreconditionFailed(Exception):
        pass

    exceptions = types.ModuleType("google.api_core.exceptions")
    exceptions.NotFound = NotFound
    exceptions.PreconditionFailed = PreconditionFailed
    monkeypatch.setitem(sys.modules, "google", types.ModuleType("google"))
    monkeypatch.setitem(sys.modules, "google.api_core", types.ModuleType("google.api_core"))
    monkeypatch.setitem(sys.modules, "google.api_core.exceptions", exceptions)

    class Blob:
        generation = 7

        def download_as_bytes(self, **request):
            self.request = request
            return b'{"format": 1, "values": {"count": 2}}'

    blob = Blob()
    backend = state._GoogleCloudBackend.__new__(state._GoogleCloudBackend)
    backend.bucket = types.SimpleNamespace(get_blob=lambda _key: blob)
    backend.key = "apps/report.json"
    document, generation = backend.read()
    assert document["values"] == {"count": 2}
    assert generation == 7
    assert blob.request["if_generation_match"] == 7


def test_azure_conditional_write(monkeypatch):
    class MatchConditions:
        IfNotModified = "if-not-modified"

    core = types.ModuleType("azure.core")
    core.MatchConditions = MatchConditions
    exceptions = types.ModuleType("azure.core.exceptions")
    exceptions.ResourceExistsError = Exception
    exceptions.ResourceModifiedError = Exception
    monkeypatch.setitem(sys.modules, "azure", types.ModuleType("azure"))
    monkeypatch.setitem(sys.modules, "azure.core", core)
    monkeypatch.setitem(sys.modules, "azure.core.exceptions", exceptions)

    class Blob:
        def upload_blob(self, payload, **request):
            self.payload = json.loads(payload)
            self.request = request

    backend = state._AzureBlobBackend.__new__(state._AzureBlobBackend)
    backend.blob = Blob()
    assert backend.write({"format": 1, "values": {}}, '"etag"')
    assert backend.blob.request["etag"] == '"etag"'
