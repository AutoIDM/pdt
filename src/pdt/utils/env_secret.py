"""Change one env var of a deployed app while it runs.

    from pdt.utils import env_secret
    env_secret.update("TAP_SALESFORCE_REFRESH_TOKEN", new_token)

PDT_ENV_SECRET_RESOURCE, set by deploy, names the app's env secret (see
deploy_common.py). Without it, on the user's computer, `update` writes
the nearest .env and then `pdt secrets <app> set`, because a rotated
credential has one current value and the deployed job needs it too.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from pdt import config
from pdt.utils.log import log

RESOURCE_ENV = "PDT_ENV_SECRET_RESOURCE"


def private_file(path: Path) -> None:
    """Make the file that holds secrets readable by its owner alone, creating it if missing."""
    path.touch(mode=0o600, exist_ok=True)
    # On Windows chmod only sets the read-only flag, so the folder's access list decides.
    if os.name != "nt":
        os.chmod(path, 0o600)


def deployed() -> bool:
    return os.environ.get(RESOURCE_ENV, "").strip() != ""


def current() -> str:
    """The deployed secret as it is now, or "" when it cannot be read."""
    try:
        return json.dumps(backend().read(), sort_keys=True)
    except Exception as e:  # noqa: BLE001 - any SDK or permission error means the mounted copy stands
        log("warning", "could not read the deployed secret; using the value the job started with",
            error=str(e))
        return ""


def update(name: str, new_value: str) -> None:
    store = backend()
    values = store.read()
    values[name] = new_value
    store.write(values)
    os.environ[name] = new_value
    os.environ["PDT_ENV_JSON"] = json.dumps(values, sort_keys=True)
    log("info", "env var stored", name=name, target=store.describe())
    if isinstance(store, EnvFile):
        set_deployed(name, new_value)


def set_deployed(name: str, new_value: str) -> None:
    app = config.running_app_dir().name
    try:
        project = config.find_project()
    except config.ConfigError:
        log("info", "no pdt project here, so no deployed secret to update", name=name)
        return
    command = [sys.executable, "-m", "pdt.cli", "secrets", app, "set", name, "--yes"]
    done = subprocess.run(command, input=new_value, capture_output=True, text=True,
                          cwd=project, env=dict(os.environ, NO_COLOR="1"))
    for line in (done.stdout + done.stderr).splitlines():
        if line.strip():
            log("info", line.strip())
    if done.returncode != 0:
        raise RuntimeError(f"pdt secrets {app} set {name} failed with exit code {done.returncode}")


class Backend:
    def __init__(self, resource: str):
        self.resource = resource

    def describe(self) -> str:
        return self.resource

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
        # Every kept version is billed.
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
        # The tags mark pdt's ownership.
        client = self.client()
        tags = client.get_secret(self.name).properties.tags
        added = client.set_secret(self.name, json.dumps(values, sort_keys=True), tags=tags)
        for version in client.list_properties_of_secret_versions(self.name):
            if version.version != added.properties.version and version.enabled:
                client.update_secret_properties(self.name, version.version, enabled=False)


class EnvFile(Backend):
    """No cloud secret: the nearest .env file that holds the var."""

    def __init__(self, resource: str):
        super().__init__(resource)
        self.written: list[Path] = []

    def files(self) -> list[Path]:
        folder = config.running_app_dir()
        return config.find_env_files(folder) or [folder / ".env"]

    def describe(self) -> str:
        return ", ".join(str(path) for path in self.written) or "no .env file (value unchanged)"

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
            private_file(target)
            set_key(str(target), name, new, quote_mode="never")
            self.written.append(target)


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
