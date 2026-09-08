import json
import base64
import hashlib
import hmac
import os
import shutil
import subprocess
import sys
import types

from pathlib import Path

import pytest

from pdt.terraform import TerraformError
from pdt.terraform_store import AzureStore, GCSStore, HTTPStore, S3Store, Store


class MemoryStore(Store):
    name = "memory"

    def __init__(self):
        super().__init__({}, {}, True)
        self.values = {}
        self.deleted = []

    def prepare(self):
        pass

    def backend(self, key):
        return {}

    def read(self, key):
        return self.values.get(key)

    def read_version(self, key):
        return self.read(key), "v1"

    def write(self, key, value, exclusive=False):
        if exclusive and key in self.values:
            raise TerraformError("exists")
        self.values[key] = value
        return "v1"

    def delete(self, key, version=None):
        self.deleted.append((key, version))
        self.values.pop(key, None)

    def list(self):
        return list(self.values)


def test_operation_lock_rejects_another_deployment():
    store = MemoryStore()
    store.values["operation.lock"] = b'{"id": "other"}'

    with pytest.raises(TerraformError, match="cloud deployment lock"):
        store.acquire()


def test_teardown_marker_blocks_deployment_even_without_a_lock(monkeypatch):
    monkeypatch.delenv("PDT_TF_UNLOCK", raising=False)
    store = MemoryStore()
    store.acquire()

    with pytest.raises(TerraformError, match="being removed"):
        store.require_active({"pdt-state-status": "deleting"})


def test_teardown_recovery_requires_one_confirmation(monkeypatch):
    store = MemoryStore()
    store.values["operation.lock"] = b'{"id": "other"}'
    prompts = []
    monkeypatch.setenv("PDT_TF_UNLOCK", "1")
    monkeypatch.setattr("builtins.input", lambda prompt: prompts.append(prompt) or "y")

    store.require_active({"pdt-state-status": "deleting"})
    store.acquire()

    assert len(prompts) == 1
    assert store.env["PDT_TF_UNLOCK_CONFIRMED"] == "1"


def test_release_only_removes_its_own_lock():
    store = MemoryStore()
    store.acquire()
    store.values["operation.lock"] = b'{"id": "other"}'

    store.release()

    assert store.deleted == []


def test_cleanup_keeps_storage_when_another_state_has_resources():
    store = MemoryStore()
    store.values["other/terraform.tfstate"] = json.dumps({"resources": [{
        "mode": "managed", "instances": [{}]}]}).encode()

    store.cleanup()

    assert store.deleted == []


def test_cleanup_keeps_storage_when_another_deployment_is_saved():
    store = MemoryStore()
    store.values["scopes/a/deployment.json"] = b"{}"

    store.cleanup()

    assert store.deleted == []


def test_gcs_state_bucket_has_pdt_ownership_label():
    store = GCSStore({"project": "example", "region": "us-central1"},
                     {"GOOGLE_OAUTH_ACCESS_TOKEN": "token"}, True)

    assert store.name == "pdt-tfstate-example"
    assert store.headers()["Authorization"] == "Bearer token"


def test_existing_bootstrap_waits_for_the_cloud_lock(tmp_path, monkeypatch):
    import pdt.terraform_store as stores

    class Workspace:
        def __init__(self, directory, configuration, **_kwargs):
            self.directory = Path(directory)
            self.directory.mkdir(parents=True, exist_ok=True)
            self.directory.joinpath("terraform.tfstate").write_text("{}")

        def init(self):
            pass

        def import_resources(self, _imports):
            pass

        def plan(self):
            return object()

        def apply(self, _plan):
            pass

    monkeypatch.setattr(stores, "TerraformWorkspace", Workspace)
    monkeypatch.setattr(stores.config, "find_project", lambda: tmp_path)
    store = MemoryStore()
    store.values["bootstrap.tfstate"] = b"{}"

    store.initialize("google", {}, {}, {}, existing=True)

    assert store.pending is not None
    assert store.bootstrap is None
    store.acquire()
    store.finalize_prepare()
    assert store.pending is None
    assert store.values["bootstrap.tfstate"] == b"{}"


def test_cleanup_refuses_unrecognized_storage_files():
    store = MemoryStore()
    store.values["notes.txt"] = b"do not delete"

    with pytest.raises(TerraformError, match="could not load"):
        store.cleanup()

    assert store.deleted == []


def test_cleanup_keeps_bootstrap_snapshot_until_native_destroy_succeeds(tmp_path):
    class Bootstrap:
        configuration = {"resource": {"google_storage_bucket": {"state": {"labels": {"managed-by": "pdt"}}}}}
        directory = tmp_path

        def reconcile(self, configuration):
            assert configuration["resource"]["google_storage_bucket"]["state"]["labels"]["pdt-state-status"] == "deleting"
            (self.directory / "terraform.tfstate").write_bytes(b"marked-state")

        def plan(self, destroy=False):
            assert destroy is True
            return "destroy-plan"

        def apply(self, plan):
            assert plan == "destroy-plan"

    store = MemoryStore()
    store.bootstrap = Bootstrap()
    store.values["bootstrap.tfstate"] = b"state"
    store.values["operation.lock"] = b"lock"

    store.cleanup()

    assert store.deleted == []
    assert store.removed is True
    assert store.values["bootstrap.tfstate"] == b"marked-state"


def test_unlock_deletes_only_the_lock_version_that_was_reviewed(monkeypatch):
    class VersionedStore(MemoryStore):
        def read_version(self, key):
            return self.values.get(key), "old-version"

        def delete(self, key, version=None):
            if key == "operation.lock" and version != "old-version":
                raise TerraformError("lock changed before removal")
            super().delete(key, version)

    store = VersionedStore()
    store.values["operation.lock"] = b'{"id": "other"}'
    monkeypatch.setenv("PDT_TF_UNLOCK", "1")
    monkeypatch.setattr("builtins.input", lambda _prompt: "y")

    store.acquire()

    assert store.lock_version == "v1"


def test_azure_shared_key_signature_keeps_empty_query_values(monkeypatch):
    key = base64.b64encode(b"k" * 32).decode()
    store = AzureStore({"subscription": "sub", "region": "eastus"}, {}, True)
    store.access_key = key
    monkeypatch.setattr(store, "headers", lambda: {
        "x-ms-date": "Mon, 01 Jan 2024 00:00:00 GMT", "x-ms-version": "2023-11-03"})
    captured = {}

    def request(_self, method, url, data, headers, missing):
        captured.update(method=method, url=url, headers=headers, missing=missing)
        return b"", {}

    monkeypatch.setattr(HTTPStore, "request", request)
    url = store.url + "?restype=container&comp=list&marker="
    store.request("GET", url)

    canonical_headers = "x-ms-date:Mon, 01 Jan 2024 00:00:00 GMT\nx-ms-version:2023-11-03\n"
    string = ("GET\n\n\n\n\n\n\n\n\n\n\n\n" + canonical_headers +
              f"/{store.name}/state\ncomp:list\nmarker:\nrestype:container")
    expected = base64.b64encode(hmac.new(base64.b64decode(key), string.encode(), hashlib.sha256).digest()).decode()

    assert captured["headers"]["Authorization"] == f"SharedKey {store.name}:{expected}"


def test_azure_existing_account_missing_state_container_reconciles_without_container_import(monkeypatch):
    store = AzureStore({"subscription": "sub", "region": "centralus"}, {}, True)
    captured = {}

    def cli(*args):
        if args[:2] == ("group", "list"):
            return [{"name": "pdt-state", "location": "westus", "tags": {"managed-by": "pdt"}}]
        if args[:3] == ("storage", "account", "list"):
            return [{"name": store.name, "location": "eastus2", "tags": {"managed-by": "pdt"}}]
        if args[:4] == ("storage", "account", "keys", "list"):
            return [{"value": base64.b64encode(b"k" * 32).decode()}]
        raise AssertionError(args)

    monkeypatch.setattr(store, "cli", cli)
    monkeypatch.setattr(store, "request", lambda *_args, **_kwargs: (None, {}))
    monkeypatch.setattr(store, "initialize", lambda provider, settings, resources, imports, existing:
                        captured.update(provider=provider, settings=settings, resources=resources,
                                        imports=imports, existing=existing))

    store.prepare()

    assert captured["resources"]["azurerm_storage_account"]["state"]["location"] == "eastus2"
    assert "azurerm_storage_container.state" not in captured["imports"]
    assert captured["imports"]["azurerm_storage_account.state"].endswith("/storageAccounts/" + store.name)


@pytest.mark.skipif(os.environ.get("PDT_RUN_TERRAFORM_PROVIDER_VALIDATE") != "1",
                    reason="set PDT_RUN_TERRAFORM_PROVIDER_VALIDATE=1 to validate cached providers")
def test_bootstrap_configs_validate_against_cached_provider_schemas(tmp_path, monkeypatch):
    import pdt.terraform_store as stores

    captured = {}
    monkeypatch.setattr(stores.Store, "initialize", lambda self, provider, settings, resources, imports, existing:
                        captured.setdefault(provider, (settings, resources)))
    gcs = GCSStore({"project": "project", "region": "us-central1"},
                   {"GOOGLE_OAUTH_ACCESS_TOKEN": "token"}, True)
    monkeypatch.setattr(gcs, "request", lambda *_args, **_kwargs: (None, {}))
    gcs.prepare()
    azure = AzureStore({"subscription": "sub", "region": "eastus"}, {}, True)
    monkeypatch.setattr(azure, "cli", lambda *args: [] if args[0] in ("group", "storage") else {})
    azure.prepare()

    class ClientError(Exception):
        def __init__(self):
            self.response = {"Error": {"Code": "NoSuchBucket"}}

    class Client:
        def get_bucket_tagging(self, **_kwargs):
            raise ClientError()

    boto3 = types.ModuleType("boto3")
    boto3.Session = lambda **_kwargs: types.SimpleNamespace(client=lambda _service: Client())
    botocore = types.ModuleType("botocore")
    exceptions = types.ModuleType("botocore.exceptions")
    exceptions.ClientError = ClientError
    botocore.exceptions = exceptions
    monkeypatch.setitem(sys.modules, "boto3", boto3)
    monkeypatch.setitem(sys.modules, "botocore", botocore)
    monkeypatch.setitem(sys.modules, "botocore.exceptions", exceptions)
    S3Store({"account": "123456789012", "region": "us-east-1"}, {}, True).prepare()

    binary = shutil.which("terraform")
    assert binary
    schema_root = Path(".secrets/debug/terraform-migration/schemas")
    for provider, (settings, resources) in captured.items():
        directory = tmp_path / provider
        source = schema_root / provider
        shutil.copytree(source / ".terraform", directory / ".terraform")
        shutil.copy(source / ".terraform.lock.hcl", directory / ".terraform.lock.hcl")
        configuration = stores.configuration(provider, settings, resources)
        (directory / "main.tf.json").write_text(json.dumps(configuration))
        result = subprocess.run([binary, f"-chdir={directory}", "validate", "-no-color"],
                                capture_output=True, text=True)
        assert result.returncode == 0, result.stderr or result.stdout
