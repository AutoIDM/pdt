"""One env var of a deployed app, changed while the app runs.

Used as a library (`from pdt.utils import env_secret`).

A cloud job holds its env vars as one json secret, mounted as
PDT_ENV_JSON (see deploy_common.py). PDT_ENV_SECRET_RESOURCE names that
secret, and deploy grants the job the right to update it. An app that
rotates a credential at run time, such as a Salesforce refresh token,
calls `update` so the next run starts with the new value.

    from pdt.utils import env_secret
    token = env_secret.value("TAP_SALESFORCE_REFRESH_TOKEN")
    env_secret.update("TAP_SALESFORCE_REFRESH_TOKEN", new_token)

`value` reads the secret itself, not this process's environment, because
the environment was fixed when the run started and an earlier update in
the same run has already changed the secret.

Without PDT_ENV_SECRET_RESOURCE (`pdt run` on the user's own computer and
the windows provider) the .env file stands in for the secret: `value`
reads the nearest .env that holds the var, and `update` writes it there,
so the next run and the next deploy both see the new value.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from pdt import config

RESOURCE_ENV = "PDT_ENV_SECRET_RESOURCE"


def deployed() -> bool:
    return os.environ.get(RESOURCE_ENV, "").strip() != ""


def value(name: str) -> str | None:
    values = backend().read()
    current = values.get(name)
    return None if current in (None, "") else str(current)


def update(name: str, new_value: str) -> None:
    store = backend()
    values = store.read()
    values[name] = new_value
    store.write(values)
    os.environ[name] = new_value
    os.environ["PDT_ENV_JSON"] = json.dumps(values, sort_keys=True)


class Backend:
    def __init__(self, resource: str):
        self.resource = resource

    def read(self) -> dict[str, str]:
        raise NotImplementedError

    def write(self, values: dict[str, str]) -> None:
        raise NotImplementedError


class SecretsManager(Backend):
    """AWS: arn:aws:secretsmanager:<region>:<account>:secret:<name>."""

    def client(self):
        import boto3
        return boto3.client("secretsmanager")

    def read(self) -> dict[str, str]:
        current = self.client().get_secret_value(SecretId=self.resource)
        return json.loads(current.get("SecretString") or "{}")

    def write(self, values: dict[str, str]) -> None:
        self.client().put_secret_value(
            SecretId=self.resource, SecretString=json.dumps(values, sort_keys=True))


class SecretManager(Backend):
    """Google Cloud: projects/<project>/secrets/<name>."""

    def client(self):
        from google.cloud import secretmanager
        return secretmanager.SecretManagerServiceClient()

    def read(self) -> dict[str, str]:
        version = self.client().access_secret_version(name=f"{self.resource}/versions/latest")
        return json.loads(version.payload.data.decode() or "{}")

    def write(self, values: dict[str, str]) -> None:
        # Every kept version is billed, so the one just added is the only one left.
        client = self.client()
        added = client.add_secret_version(request={
            "parent": self.resource,
            "payload": {"data": json.dumps(values, sort_keys=True).encode()},
        })
        for version in client.list_secret_versions(
                request={"parent": self.resource, "filter": "state:ENABLED"}):
            if version.name != added.name:
                client.destroy_secret_version(name=version.name)


class KeyVault(Backend):
    """Azure: https://<vault>.vault.azure.net/secrets/<name>."""

    def __init__(self, resource: str):
        super().__init__(resource)
        self.vault_url, self.name = resource.rstrip("/").split("/secrets/", 1)

    def client(self):
        from azure.identity import DefaultAzureCredential
        from azure.keyvault.secrets import SecretClient
        return SecretClient(self.vault_url, DefaultAzureCredential())

    def read(self) -> dict[str, str]:
        return json.loads(self.client().get_secret(self.name).value or "{}")

    def write(self, values: dict[str, str]) -> None:
        # The tags carry pdt's ownership mark, and deploy keeps one enabled version.
        client = self.client()
        tags = client.get_secret(self.name).properties.tags
        added = client.set_secret(self.name, json.dumps(values, sort_keys=True), tags=tags)
        for version in client.list_properties_of_secret_versions(self.name):
            if version.version != added.properties.version and version.enabled:
                client.update_secret_properties(self.name, version.version, enabled=False)


class EnvFile(Backend):
    """No cloud secret: the nearest .env file that holds the var."""

    def files(self) -> list[Path]:
        files = config.find_env_files(Path.cwd())
        return files or [Path.cwd() / ".env"]

    def read(self) -> dict[str, str]:
        from dotenv import dotenv_values
        values: dict[str, str] = {}
        for path in reversed(self.files()):
            if path.is_file():
                values.update({k: v for k, v in dotenv_values(path).items() if v is not None})
        return values

    def write(self, values: dict[str, str]) -> None:
        from dotenv import dotenv_values, set_key
        current = self.read()
        changed = {name: new for name, new in values.items() if current.get(name) != new}
        for name, new in changed.items():
            target = next((path for path in self.files()
                           if path.is_file() and name in dotenv_values(path)), self.files()[0])
            target.touch(mode=0o600, exist_ok=True)
            set_key(str(target), name, new, quote_mode="never")


def backend(resource: str | None = None) -> Backend:
    resource = (os.environ.get(RESOURCE_ENV, "") if resource is None else resource).strip()
    if resource.startswith("arn:aws:secretsmanager:"):
        return SecretsManager(resource)
    if resource.startswith("projects/"):
        return SecretManager(resource)
    if resource.startswith("https://") and "/secrets/" in resource:
        return KeyVault(resource)
    if resource == "":
        return EnvFile(resource)
    raise ValueError(f"{RESOURCE_ENV} names no secret store pdt knows: {resource}")
