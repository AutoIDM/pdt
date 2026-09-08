"""Cloud storage for Terraform state and deployment operation locks."""

from __future__ import annotations

import base64
import datetime
import email.utils
import hashlib
import hmac
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

from pdt import config, console
from pdt.terraform import TerraformError, TerraformWorkspace, atomic_write, configuration


class Store:
    def __init__(self, identity: dict, env: dict, assume_yes: bool):
        self.identity = identity
        self.env = env
        self.assume_yes = assume_yes
        self.owner = uuid.uuid4().hex
        self.lock_version = None
        self.removed = False
        self.bootstrap = None
        self.pending = None

    def prepare(self) -> None:
        raise NotImplementedError

    def read_version(self, key: str) -> tuple[bytes | None, str | None]:
        raise NotImplementedError

    def check_teardown(self) -> None:
        pass

    def require_active(self, metadata: dict) -> None:
        if metadata.get("pdt-state-status") != "deleting":
            return
        accepted = self.env.get("PDT_TF_UNLOCK_CONFIRMED") == "1"
        if not accepted and os.environ.get("PDT_TF_UNLOCK") == "1":
            try:
                accepted = input("The previous removal must have stopped. Recover its state storage? [y/N] ").strip().lower() in ("y", "yes")
            except EOFError:
                accepted = False
            if accepted:
                self.env["PDT_TF_UNLOCK_CONFIRMED"] = "1"
        if not accepted:
            raise TerraformError("Terraform state storage is being removed. After that operation stops, repeat this command with --unlock to recover.")

    def acquire(self) -> None:
        raw = json.dumps({"id": self.owner, "host": socket.gethostname(), "pid": os.getpid()}).encode()
        try:
            self.lock_version = self.write("operation.lock", raw, exclusive=True)
        except TerraformError as exc:
            existing, version = self.read_version("operation.lock")
            if existing is None:
                raise
            if os.environ.get("PDT_TF_UNLOCK") == "1":
                accepted = self.env.get("PDT_TF_UNLOCK_CONFIRMED") == "1"
                if not accepted:
                    try:
                        accepted = input("The previous deployment must have stopped. Remove its lock? [y/N] ").strip().lower() in ("y", "yes")
                    except EOFError:
                        accepted = False
                if accepted:
                    if version is None:
                        raise TerraformError("The lock has no version; PDT cannot safely remove it")
                    self.delete("operation.lock", version=version)
                    self.lock_version = self.write("operation.lock", raw, exclusive=True)
                    self.env["PDT_TF_UNLOCK_CONFIRMED"] = "1"
                    return
            raise TerraformError("Another PDT operation holds the cloud deployment lock. "
                                 "After it stops, repeat this command with --unlock to recover.") from exc

    def release(self) -> None:
        if self.removed or self.lock_version is None:
            return
        raw = self.read("operation.lock")
        if raw and json.loads(raw).get("id") == self.owner:
            self.delete("operation.lock", version=self.lock_version)
        self.lock_version = None

    def initialize(self, provider: str, settings: dict, resources: dict, imports: dict,
                   existing: bool) -> None:
        if existing and self.lock_version is None:
            self.pending = (provider, settings, resources, imports)
            return
        root = config.find_project() / ".pdt" / "terraform" / "bootstrap" / self.name
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if existing:
            state = self.read("bootstrap.tfstate")
            if state:
                atomic_write(root / "terraform.tfstate", state)
        self.bootstrap = TerraformWorkspace(root, configuration(provider, settings, resources), env=self.env,
                                            assume_yes=self.assume_yes)
        self.bootstrap.init()
        self.bootstrap.import_resources(imports)
        if not existing:
            self.bootstrap.apply(self.bootstrap.plan())
            self.write("bootstrap.tfstate", (root / "terraform.tfstate").read_bytes())

    def finalize_prepare(self) -> None:
        if self.pending:
            self.initialize(*self.pending, existing=True)
            self.pending = None
        if self.bootstrap is None:
            raise TerraformError("PDT could not load the Terraform state storage definition")
        self.bootstrap.apply(self.bootstrap.plan())
        state = self.bootstrap.directory / "terraform.tfstate"
        if not state.is_file():
            raise TerraformError("Terraform did not write the state storage bootstrap state")
        self.write("bootstrap.tfstate", state.read_bytes())

    def cleanup(self) -> None:
        keys = self.list()
        for key in keys:
            if key.endswith(".tfstate") and key != "bootstrap.tfstate":
                raw = self.read(key)
                if raw:
                    state = json.loads(raw)
                    if any(resource.get("mode") == "managed" and resource.get("instances")
                           for resource in state.get("resources", [])):
                        return
        if any(key.endswith("/deployment.json") or key.startswith("owners/") for key in keys):
            return
        if self.bootstrap is None:
            raise TerraformError("PDT could not load the state storage definition for cleanup")
        allowed = ("scopes/", "shared/", "global/")
        if any(key not in ("operation.lock", "bootstrap.tfstate") and not key.startswith(allowed) for key in keys):
            console.note("Terraform state storage contains other files; PDT kept it.")
            return
        console.step("removing unused Terraform state storage")
        for instances in self.bootstrap.configuration.get("resource", {}).values():
            for resource in instances.values():
                for key in ("tags", "labels"):
                    if key in resource:
                        resource[key]["pdt-state-status"] = "deleting"
        self.bootstrap.reconcile(self.bootstrap.configuration)
        state = self.bootstrap.directory / "terraform.tfstate"
        self.write("bootstrap.tfstate", state.read_bytes())
        self.bootstrap.apply(self.bootstrap.plan(destroy=True))
        self.removed = True


class S3Store(Store):
    def __init__(self, identity: dict, env: dict, assume_yes: bool):
        super().__init__(identity, env, assume_yes)
        import boto3
        self.region = identity["region"]
        self.name = "pdt-tfstate-" + identity["account"]
        self.client = boto3.Session(
            aws_access_key_id=env.get("AWS_ACCESS_KEY_ID"),
            aws_secret_access_key=env.get("AWS_SECRET_ACCESS_KEY"),
            aws_session_token=env.get("AWS_SESSION_TOKEN"),
            profile_name=env.get("AWS_PROFILE"), region_name=self.region).client("s3")

    def prepare(self) -> None:
        from botocore.exceptions import ClientError
        try:
            tags = self.client.get_bucket_tagging(Bucket=self.name)["TagSet"]
        except ClientError as exc:
            code = exc.response["Error"]["Code"]
            if code != "NoSuchBucket":
                raise TerraformError(f"Cannot use state bucket {self.name}: {code}. "
                                     "The deployer needs s3:GetBucketTagging, s3:GetBucketLocation, "
                                     "s3:ListBucket, s3:GetObject, s3:PutObject and s3:DeleteObject.") from exc
            exists = False
        else:
            if {tag["Key"]: tag["Value"] for tag in tags}.get("managed-by") != "pdt":
                raise TerraformError(f"State bucket {self.name} exists but is not managed by PDT")
            self.require_active({tag["Key"]: tag["Value"] for tag in tags})
            self.region = self.client.get_bucket_location(Bucket=self.name).get("LocationConstraint") or "us-east-1"
            if self.region == "EU":
                self.region = "eu-west-1"
            exists = True
        resources = {
            "aws_s3_bucket": {"state": {"bucket": self.name, "force_destroy": True, "tags": {"managed-by": "pdt"}}},
            "aws_s3_bucket_versioning": {"state": {"bucket": "${aws_s3_bucket.state.id}", "versioning_configuration": {"status": "Enabled"}}},
            "aws_s3_bucket_public_access_block": {"state": {"bucket": "${aws_s3_bucket.state.id}", "block_public_acls": True,
                "block_public_policy": True, "ignore_public_acls": True, "restrict_public_buckets": True}},
            "aws_s3_bucket_server_side_encryption_configuration": {"state": {"bucket": "${aws_s3_bucket.state.id}",
                "rule": {"apply_server_side_encryption_by_default": {"sse_algorithm": "AES256"}}}},
        }
        imports = {kind + ".state": self.name for kind in resources} if exists else {}
        self.initialize("aws", {"region": self.region, "allowed_account_ids": [self.identity["account"]]}, resources, imports, exists)

    def check_teardown(self) -> None:
        tags = self.client.get_bucket_tagging(Bucket=self.name)["TagSet"]
        self.require_active({tag["Key"]: tag["Value"] for tag in tags})

    def backend(self, key: str) -> dict:
        return {"s3": {"bucket": self.name, "region": self.region, "key": key, "use_lockfile": True, "encrypt": True}}

    def read(self, key: str) -> bytes | None:
        return self.read_version(key)[0]

    def read_version(self, key: str) -> tuple[bytes | None, str | None]:
        from botocore.exceptions import ClientError
        try:
            response = self.client.get_object(Bucket=self.name, Key=key)
            return response["Body"].read(), response["ETag"]
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "NoSuchKey":
                return None, None
            raise TerraformError(f"Cannot read Terraform state storage: {exc.response['Error']['Code']}") from exc

    def write(self, key: str, value: bytes, exclusive: bool = False):
        from botocore.exceptions import ClientError
        kwargs = {"IfNoneMatch": "*"} if exclusive else {}
        try:
            return self.client.put_object(Bucket=self.name, Key=key, Body=value, **kwargs)["ETag"]
        except ClientError as exc:
            raise TerraformError(f"Cannot write Terraform state storage: {exc.response['Error']['Code']}") from exc

    def delete(self, key: str, version=None) -> None:
        kwargs = {"IfMatch": version} if version else {}
        self.client.delete_object(Bucket=self.name, Key=key, **kwargs)

    def list(self) -> list[str]:
        return [item["Key"] for page in self.client.get_paginator("list_objects_v2").paginate(Bucket=self.name)
                for item in page.get("Contents", [])]


class HTTPStore(Store):
    def request(self, method: str, url: str, data: bytes | None = None,
                headers: dict | None = None, missing: bool = False):
        request = urllib.request.Request(url, data=data, method=method, headers=self.headers() | (headers or {}))
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                return response.read(), dict(response.headers)
        except urllib.error.HTTPError as exc:
            if missing and exc.code == 404:
                return None, {}
            raise TerraformError(f"State storage {method} failed with HTTP {exc.code}; "
                                 "check the deployment account's storage permissions") from exc


class GCSStore(HTTPStore):
    def __init__(self, identity: dict, env: dict, assume_yes: bool):
        super().__init__(identity, env, assume_yes)
        self.name = "pdt-tfstate-" + identity["project"]
        self.url = "https://storage.googleapis.com/storage/v1/b/" + self.name

    def headers(self) -> dict:
        token = self.env.get("GOOGLE_OAUTH_ACCESS_TOKEN")
        if not token:
            raise TerraformError("Google login did not provide a token for Terraform state storage")
        return {"Authorization": "Bearer " + token}

    def prepare(self) -> None:
        raw, _ = self.request("GET", self.url, missing=True)
        exists = raw is not None
        region = self.identity["region"]
        if raw:
            bucket = json.loads(raw)
            if bucket.get("labels", {}).get("managed-by") != "pdt":
                raise TerraformError(f"State bucket {self.name} exists but is not managed by PDT")
            self.require_active(bucket.get("labels", {}))
            region = bucket["location"]
        resources = {"google_storage_bucket": {"state": {"name": self.name, "project": self.identity["project"],
            "location": region, "force_destroy": True, "uniform_bucket_level_access": True,
            "public_access_prevention": "enforced", "versioning": {"enabled": True},
            "soft_delete_policy": {"retention_duration_seconds": 0}, "labels": {"managed-by": "pdt"}}}}
        self.initialize("google", {"project": self.identity["project"]}, resources,
                        {"google_storage_bucket.state": self.name} if exists else {}, exists)

    def check_teardown(self) -> None:
        raw, _ = self.request("GET", self.url)
        self.require_active(json.loads(raw).get("labels", {}))

    def backend(self, key: str) -> dict:
        return {"gcs": {"bucket": self.name, "prefix": key.removesuffix("/terraform.tfstate")}}

    def read(self, key: str) -> bytes | None:
        raw, _ = self.request("GET", self.url + "/o/" + urllib.parse.quote(key, safe="") + "?alt=media", missing=True)
        return raw

    def read_version(self, key: str) -> tuple[bytes | None, str | None]:
        url = self.url + "/o/" + urllib.parse.quote(key, safe="")
        raw, _ = self.request("GET", url, missing=True)
        if raw is None:
            return None, None
        generation = json.loads(raw)["generation"]
        raw, _ = self.request("GET", url + "?alt=media&generation=" + generation, missing=True)
        return raw, generation

    def write(self, key: str, value: bytes, exclusive: bool = False):
        params = {"uploadType": "media", "name": key}
        if exclusive:
            params["ifGenerationMatch"] = "0"
        url = "https://storage.googleapis.com/upload/storage/v1/b/" + self.name + "/o?" + urllib.parse.urlencode(params)
        raw, _ = self.request("POST", url, value, {"Content-Type": "application/json"})
        return json.loads(raw)["generation"]

    def delete(self, key: str, version=None) -> None:
        url = self.url + "/o/" + urllib.parse.quote(key, safe="")
        if version:
            url += "?ifGenerationMatch=" + str(version)
        self.request("DELETE", url, missing=True)

    def list(self) -> list[str]:
        result = []
        token = ""
        while True:
            raw, _ = self.request("GET", self.url + "/o?" + urllib.parse.urlencode({"pageToken": token}))
            page = json.loads(raw)
            result.extend(item["name"] for item in page.get("items", []))
            token = page.get("nextPageToken", "")
            if not token:
                return result


class AzureStore(HTTPStore):
    def __init__(self, identity: dict, env: dict, assume_yes: bool):
        super().__init__(identity, env, assume_yes)
        self.name = "pdttf" + hashlib.sha256(identity["subscription"].encode()).hexdigest()[:19]
        self.group = "pdt-state"
        self.url = "https://" + self.name + ".blob.core.windows.net/state"
        self.access_key = None

    def cli(self, *args: str):
        result = subprocess.run([sys.executable, "-m", "azure.cli", *args, "--output", "json"],
                                capture_output=True, text=True, env=dict(os.environ, **self.env))
        if result.returncode:
            raise TerraformError("Azure could not access Terraform state storage: " + result.stderr.strip())
        return json.loads(result.stdout)

    def headers(self) -> dict:
        return {"x-ms-version": "2023-11-03",
                "x-ms-date": email.utils.format_datetime(datetime.datetime.now(datetime.UTC), usegmt=True)}

    def request(self, method: str, url: str, data: bytes | None = None,
                headers: dict | None = None, missing: bool = False):
        if self.access_key is None:
            keys = self.cli("storage", "account", "keys", "list", "--account-name", self.name,
                            "--resource-group", self.group, "--subscription", self.identity["subscription"])
            self.access_key = keys[0]["value"]
            self.env["ARM_ACCESS_KEY"] = self.access_key
        headers = self.headers() | (headers or {})
        length = str(len(data)) if data else ""
        fields = [method, "", "", length, "", headers.get("Content-Type", ""), "", "",
                  headers.get("If-Match", ""), headers.get("If-None-Match", ""), "", ""]
        canonical_headers = "".join(f"{key.lower()}:{value}\n" for key, value in sorted(headers.items(), key=lambda pair: pair[0].lower())
                                    if key.lower().startswith("x-ms-"))
        parsed = urllib.parse.urlsplit(url)
        resource = "/" + self.name + parsed.path
        for key, values in sorted(urllib.parse.parse_qs(parsed.query, keep_blank_values=True).items()):
            resource += "\n" + key.lower() + ":" + ",".join(sorted(values))
        message = "\n".join(fields) + "\n" + canonical_headers + resource
        signature = base64.b64encode(hmac.new(base64.b64decode(self.access_key), message.encode(), hashlib.sha256).digest()).decode()
        headers["Authorization"] = f"SharedKey {self.name}:{signature}"
        for wait in (2, 5, 10, 0):
            try:
                return super().request(method, url, data, headers, missing)
            except TerraformError as exc:
                if wait == 0 or "HTTP 403" not in str(exc):
                    raise
                time.sleep(wait)

    def prepare(self) -> None:
        subscription = self.identity["subscription"]
        groups = self.cli("group", "list", "--subscription", subscription)
        group = next((item for item in groups if item["name"] == self.group), None)
        if group and group.get("tags", {}).get("managed-by") != "pdt":
            raise TerraformError(f"Resource group {self.group} exists but is not managed by PDT")
        if group:
            self.require_active(group.get("tags", {}))
        accounts = self.cli("storage", "account", "list", "--subscription", subscription)
        account = next((item for item in accounts if item["name"] == self.name), None)
        if account and account.get("tags", {}).get("managed-by") != "pdt":
            raise TerraformError(f"Storage account {self.name} exists but is not managed by PDT")
        if account:
            self.require_active(account.get("tags", {}))
        location = (group or {}).get("location", self.identity["region"])
        account_location = (account or {}).get("location", self.identity["region"])
        prefix = f"/subscriptions/{subscription}/resourceGroups/{self.group}"
        account_id = prefix + "/providers/Microsoft.Storage/storageAccounts/" + self.name
        resources = {
            "azurerm_resource_group": {"state": {"name": self.group, "location": location, "tags": {"managed-by": "pdt"}}},
            "azurerm_storage_account": {"state": {"name": self.name, "resource_group_name": "${azurerm_resource_group.state.name}",
                "location": account_location, "account_tier": "Standard", "account_replication_type": "LRS",
                "allow_nested_items_to_be_public": False, "blob_properties": {"versioning_enabled": True}, "tags": {"managed-by": "pdt"}}},
            "azurerm_storage_container": {"state": {"name": "state", "storage_account_id": "${azurerm_storage_account.state.id}", "container_access_type": "private"}},
        }
        imports = {}
        if group:
            imports["azurerm_resource_group.state"] = prefix
        container_exists = False
        if account:
            imports["azurerm_storage_account.state"] = account_id
            container, _ = self.request("HEAD", self.url + "?restype=container", missing=True)
            container_exists = container is not None
            if container_exists:
                imports["azurerm_storage_container.state"] = account_id + "/blobServices/default/containers/state"
        self.initialize("azurerm", {"features": {}, "subscription_id": subscription,
                        "resource_provider_registrations": "none", "resource_providers_to_register": ["Microsoft.Storage"]},
                        resources, imports, container_exists)

    def check_teardown(self) -> None:
        group = self.cli("group", "show", "--name", self.group, "--subscription", self.identity["subscription"])
        self.require_active(group.get("tags", {}))
        account = self.cli("storage", "account", "show", "--name", self.name,
                           "--resource-group", self.group, "--subscription", self.identity["subscription"])
        self.require_active(account.get("tags", {}))

    def backend(self, key: str) -> dict:
        return {"azurerm": {"resource_group_name": self.group, "storage_account_name": self.name,
            "container_name": "state", "key": key, "subscription_id": self.identity["subscription"]}}

    def read(self, key: str) -> bytes | None:
        return self.read_version(key)[0]

    def read_version(self, key: str) -> tuple[bytes | None, str | None]:
        raw, headers = self.request("GET", self.url + "/" + urllib.parse.quote(key, safe="/"), missing=True)
        return raw, headers.get("ETag") or headers.get("Etag")

    def write(self, key: str, value: bytes, exclusive: bool = False):
        headers = {"x-ms-blob-type": "BlockBlob", "Content-Type": "application/json"}
        if exclusive:
            headers["If-None-Match"] = "*"
        _, response = self.request("PUT", self.url + "/" + urllib.parse.quote(key, safe="/"), value, headers)
        return response.get("ETag") or response.get("Etag")

    def delete(self, key: str, version=None) -> None:
        headers = {"x-ms-delete-snapshots": "include"}
        if version:
            headers["If-Match"] = version
        self.request("DELETE", self.url + "/" + urllib.parse.quote(key, safe="/"), headers=headers, missing=True)

    def list(self) -> list[str]:
        result = []
        marker = ""
        while True:
            raw, _ = self.request("GET", self.url + "?" + urllib.parse.urlencode({"restype": "container", "comp": "list", "marker": marker}))
            page = ET.fromstring(raw)
            result.extend(item.text for item in page.findall("./Blobs/Blob/Name"))
            marker = page.findtext("NextMarker")
            if not marker:
                return result


def cloud_store(provider: str, identity: dict, env: dict, assume_yes: bool) -> Store:
    if provider == "azure":
        directory = config.find_project() / ".pdt" / "terraform" / "bin"
        directory.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            atomic_write(directory / "az.cmd", f'@"{sys.executable}" -m azure.cli %*\r\n'.encode())
        else:
            import shlex
            script = directory / "az"
            atomic_write(script, f'#!/bin/sh\nexec {shlex.quote(sys.executable)} -m azure.cli "$@"\n'.encode())
            script.chmod(0o700)
        env["PATH"] = str(directory) + os.pathsep + env.get("PATH", os.environ.get("PATH", ""))
        env["ARM_USE_CLI"] = "true"
        env["ARM_SUBSCRIPTION_ID"] = identity["subscription"]
    return {"aws": S3Store, "azure": AzureStore, "google-cloud": GCSStore}[provider](identity, env, assume_yes)
