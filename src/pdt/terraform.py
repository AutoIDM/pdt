"""Terraform execution and durable deployment configurations."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import tempfile
import urllib.request
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path

from pdt import config, console

VERSION = "1.16.1"
PROVIDERS = {
    "aws": ("hashicorp/aws", "6.63.0"),
    "azurerm": ("hashicorp/azurerm", "5.4.0"),
    "google": ("hashicorp/google", "8.1.0"),
    "shell": ("terr4m/shell", "0.9.1"),
}
CHECKSUMS = {
    "darwin_amd64": "3f165e7fabdb8ec44151494418efa1e8095c3f589ed8376a93578a96867a062c",
    "darwin_arm64": "e22cba761ddbd4d218939b28715ab3af37aaf8a42efa41f7d75b2c3d73636060",
    "linux_amd64": "745d33b4b02b7980c62a38ec1beea24ee084ea8caf3f503c200554bd9a0cbe49",
    "linux_arm64": "423288a23ab024d42ac05c409972585f7ec0cf1be572b773ad952f9a1c41387d",
    "windows_amd64": "5c6c6d8fedf56ce29c55f0c1fc91de3c259f42c2d220a28e827b5b60fd47bfa1",
    "windows_arm64": "a1777d9e6bce7764843c23a88afaaaa68c43745dcad76be7bef0cd184c4475d9",
}


class TerraformError(config.ConfigError):
    pass


def data_directory() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData/Local")
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")
    return base / "pdt" / "terraform" / VERSION


def ensure_terraform(assume_yes: bool = False) -> str:
    system = platform.system().lower()
    machine = platform.machine().lower()
    architecture = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(machine)
    # The pinned cloud providers publish Windows binaries only for amd64.
    if system == "windows" and architecture == "arm64":
        architecture = "amd64"
    found = shutil.which("terraform")
    if found:
        result = subprocess.run([found, "version", "-json"], capture_output=True, text=True)
        try:
            installed = json.loads(result.stdout)
            compatible = system != "windows" or installed.get("platform") == "windows_amd64"
            if result.returncode == 0 and installed["terraform_version"] == VERSION and compatible:
                return found
        except (ValueError, KeyError):
            pass
    directory = data_directory()
    executable = directory / ("terraform.exe" if os.name == "nt" else "terraform")
    if executable.is_file():
        if system != "windows" or machine not in ("arm64", "aarch64"):
            return str(executable)
        result = subprocess.run([str(executable), "version", "-json"], capture_output=True, text=True)
        try:
            if result.returncode == 0 and json.loads(result.stdout).get("platform") == "windows_amd64":
                return str(executable)
        except ValueError:
            pass
    key = f"{system}_{architecture}"
    if key not in CHECKSUMS:
        raise TerraformError(f"PDT has no Terraform download for {platform.system()} {machine}")
    console.say(f"PDT needs Terraform {VERSION}. It can download it to {directory}.")
    if not assume_yes:
        try:
            accepted = input("Download now? [y/N] ").strip().lower() in ("y", "yes")
        except EOFError:
            accepted = False
        if not accepted:
            raise TerraformError("Terraform download declined; run the PDT command again to accept it")
    filename = f"terraform_{VERSION}_{key}.zip"
    directory.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=directory.parent) as temporary:
        archive = Path(temporary) / filename
        try:
            with urllib.request.urlopen(f"https://releases.hashicorp.com/terraform/{VERSION}/{filename}", timeout=120) as response:
                archive.write_bytes(response.read())
        except OSError as exc:
            raise TerraformError(f"Terraform download failed: {exc}") from exc
        if hashlib.sha256(archive.read_bytes()).hexdigest() != CHECKSUMS[key]:
            raise TerraformError("Terraform download checksum did not match the pinned release")
        with zipfile.ZipFile(archive) as package:
            binary = package.read(executable.name)
        directory.mkdir(parents=True, exist_ok=True)
        atomic_write(executable, binary)
        executable.chmod(0o755)
    return str(executable)


def atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}")
    try:
        with temporary.open("xb") as handle:
            temporary.chmod(0o600)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def configuration(provider: str, provider_settings: dict, resources: dict,
                  data: dict | None = None, outputs: dict | None = None) -> dict:
    source, version = PROVIDERS[provider]
    result = {
        "terraform": {"required_version": f"= {VERSION}",
                      "required_providers": {provider: {"source": source, "version": f"= {version}"}}},
        "provider": {provider: provider_settings},
        "resource": resources,
    }
    if data:
        result["data"] = data
    if outputs:
        result["output"] = outputs
    return result


@dataclass
class Plan:
    path: Path
    actions: list[str]


class TerraformWorkspace:
    def __init__(self, directory: Path, configuration: dict, backend: dict | None = None,
                 env: dict | None = None, assume_yes: bool = False):
        self.directory = Path(directory)
        self.configuration = configuration
        self.backend = backend or {}
        self.env = dict(os.environ, TF_IN_AUTOMATION="1", CHECKPOINT_DISABLE="1")
        self.env.update(env or {})
        self.assume_yes = assume_yes
        self.binary = ensure_terraform(assume_yes)
        self.persist = None
        self.initialized = False

    def write(self) -> None:
        content = json.loads(json.dumps(self.configuration))
        if self.backend:
            content.setdefault("terraform", {})["backend"] = self.backend
        atomic_write(self.directory / "main.tf.json", json.dumps(content, indent=2).encode())
        if self.persist:
            self.persist(self.configuration)

    def run(self, *args: str, allowed: tuple[int, ...] = (0,)) -> subprocess.CompletedProcess:
        result = subprocess.run([self.binary, f"-chdir={self.directory}", *args],
                                capture_output=True, text=True, env=self.env, stdin=subprocess.DEVNULL)
        if result.returncode not in allowed and "Error acquiring the state lock" in result.stderr and self.env.get("PDT_TF_UNLOCK") == "1":
            match = re.search(r"^\s*ID:\s+(\S+)\s*$", result.stderr, re.MULTILINE)
            if match:
                accepted = self.env.get("PDT_TF_UNLOCK_CONFIRMED") == "1"
                if not accepted:
                    try:
                        accepted = input("The previous deployment must have stopped. Remove its state lock? [y/N] ").strip().lower() in ("y", "yes")
                    except EOFError:
                        accepted = False
                if accepted:
                    self.run("force-unlock", "-force", match[1])
                    result = subprocess.run([self.binary, f"-chdir={self.directory}", *args],
                                            capture_output=True, text=True, env=self.env, stdin=subprocess.DEVNULL)
        if result.returncode not in allowed:
            detail = result.stderr.strip() or result.stdout.strip()
            for key, value in self.env.items():
                if value and len(value) > 8 and any(word in key.upper() for word in ("TOKEN", "SECRET", "PASSWORD", "ACCESS_KEY")):
                    detail = detail.replace(value, "[redacted]")
            raise TerraformError(f"Terraform {args[0]} failed: {detail}")
        return result

    def init(self) -> None:
        self.write()
        self.run("init", "-input=false", "-no-color")
        self.initialized = True

    def import_resources(self, resources: dict[str, str]) -> None:
        if not resources:
            return
        existing = set(self.run("state", "list").stdout.splitlines())
        for address, resource_id in resources.items():
            if address not in existing:
                self.run("import", "-input=false", "-no-color", address, resource_id)

    def plan(self, destroy: bool = False) -> Plan:
        if not self.initialized:
            self.init()
        path = self.directory / "operation.tfplan"
        args = ["plan", "-input=false", "-no-color", "-out=" + str(path), "-detailed-exitcode"]
        if destroy:
            args.append("-destroy")
        self.run(*args, allowed=(0, 2))
        path.chmod(0o600)
        document = json.loads(self.run("show", "-json", str(path)).stdout)
        actions = []
        for change in document.get("resource_changes", []):
            operations = change.get("change", {}).get("actions", [])
            if operations in (["no-op"], ["read"]):
                continue
            verb = "replace" if "create" in operations and "delete" in operations else ", ".join(operations)
            actions.append(f"{verb} {change['address']}")
        return Plan(path, actions)

    def apply(self, plan: Plan) -> None:
        if plan.actions:
            console.step(f"applying {len(plan.actions)} infrastructure change(s)")
        try:
            self.run("apply", "-input=false", "-no-color", str(plan.path))
        finally:
            plan.path.unlink(missing_ok=True)

    def outputs(self) -> dict:
        values = json.loads(self.run("output", "-json").stdout)
        return {key: item["value"] for key, item in values.items()}

    def reconcile(self, configuration: dict, imports: dict | None = None) -> dict:
        self.configuration = configuration
        self.init()
        self.import_resources(imports or {})
        self.apply(self.plan())
        return self.outputs()

    def remove_resources(self, addresses: list[str]) -> dict:
        for address in addresses:
            kind, name = address.split(".", 1)
            self.configuration.get("resource", {}).get(kind, {}).pop(name, None)
        return self.reconcile(self.configuration)


class Deployment:
    def __init__(self, app: dict, provider: str, identity: dict, env: dict | None = None,
                 assume_yes: bool = False):
        self.app = app
        self.provider = provider
        self.identity = identity
        self.env = env or {}
        self.assume_yes = assume_yes
        scope = {key: identity[key] for key in ("account", "subscription", "project", "resource_group", "region", "machine") if key in identity}
        self.scope = hashlib.sha256(json.dumps([provider, scope], sort_keys=True).encode()).hexdigest()[:20]
        self.root = config.find_project() / ".pdt" / "terraform"
        self.state_dir = self.root / self.scope / app["name"]
        self.store = None
        self.workspaces = []
        self.lock_path = None
        self.lock_handle = None

    def __enter__(self):
        self.check_identity()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        atomic_write(self.root.parent / ".gitignore", b"*\n")
        if self.provider != "windows":
            from pdt.terraform_store import cloud_store
            self.store = cloud_store(self.provider, self.identity, self.env, self.assume_yes)
            self.store.prepare()
            self.store.acquire()
            try:
                self.store.check_teardown()
                self.check_identity()
                self.store.finalize_prepare()
            except BaseException:
                self.store.release()
                raise
        else:
            self.lock_path = self.root / "windows.lock"
            self.lock_handle = self.lock_path.open("a+b")
            try:
                self.lock_handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(self.lock_handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                self.lock_handle.close()
                self.lock_handle = None
                raise TerraformError("Another PDT operation holds the local deployment lock. Wait for it to finish.") from exc
        return self

    def __exit__(self, *args):
        if self.store:
            try:
                self.store.release()
            except Exception:
                if args[0] is None:
                    raise
                console.note("PDT could not release the deployment lock. Use --unlock after this operation stops.")
        if self.lock_handle:
            self.lock_handle.close()

    def key(self, name: str) -> str:
        if name == "app":
            return f"scopes/{self.scope}/apps/{self.app['name']}"
        if name == "global":
            return "global"
        if self.provider == "azure":
            group = self.identity.get("resource_group", "pdt")
            return "shared/" + hashlib.sha256(group.encode()).hexdigest()[:16]
        return f"shared/{self.identity.get('region', 'local')}/{name}"

    def workspace(self, name: str, configuration: dict, imports: dict | None = None,
                  retain: bool = False) -> TerraformWorkspace:
        key = self.key(name)
        directory = self.root / "workspaces" / hashlib.sha256((self.provider + json.dumps({k: self.identity[k] for k in ("account", "subscription", "project") if k in self.identity}, sort_keys=True) + key).encode()).hexdigest()[:20]
        previous = self.read_configuration(name)
        if retain and previous:
            merged = json.loads(json.dumps(previous))
            for block, value in configuration.items():
                if block in ("resource", "data"):
                    for kind, instances in value.items():
                        if name == "shared":
                            for instance, settings in instances.items():
                                old = merged.get(block, {}).get(kind, {}).get(instance, {})
                                if old.get("location") and settings.get("location") and old["location"] != settings["location"]:
                                    raise TerraformError("platform.region differs from the shared resources' location; use a different platform.resource_group for the new region")
                        merged.setdefault(block, {}).setdefault(kind, {}).update(instances)
                elif block in ("output", "locals", "variable"):
                    merged.setdefault(block, {}).update(value)
                else:
                    merged[block] = value
            configuration = merged
        backend = self.store.backend(key + "/terraform.tfstate") if self.store else {}
        workspace = TerraformWorkspace(directory, configuration, backend, self.env, self.assume_yes)
        workspace.persist = lambda value: self.write_configuration(name, value)
        workspace.init()
        workspace.import_resources(imports or {})
        self.workspaces.append(workspace)
        return workspace

    def read_configuration(self, name: str) -> dict | None:
        key = self.key(name) + "/configuration.json"
        if self.store:
            raw = self.store.read(key)
        else:
            path = self.root / key
            raw = path.read_bytes() if path.is_file() else None
        if raw is None:
            return None
        return json.loads(raw)

    def write_configuration(self, name: str, value: dict) -> None:
        key = self.key(name) + "/configuration.json"
        raw = json.dumps(value, indent=2).encode()
        if self.store:
            self.store.write(key, raw)
        else:
            atomic_write(self.root / key, raw)

    def existing_workspace(self, name: str) -> TerraformWorkspace | None:
        value = self.read_configuration(name)
        if value is None:
            return None
        return self.workspace(name, value)

    def save(self) -> None:
        value = {"provider": self.provider, "identity": self.identity}
        value.update({key: self.app[key] for key in ("schedule", "timezone") if key in self.app})
        self.check_identity()
        if self.store:
            owner_key = "owners/" + self.app["name"] + ".json"
            self.store.write(owner_key, json.dumps(value).encode())
        atomic_write(self.root / "deployments" / (self.app["name"] + ".json"), json.dumps(value, indent=2).encode())
        if self.store:
            self.store.write(self.key("app") + "/deployment.json", json.dumps(value).encode())

    def check_identity(self) -> None:
        value = {"provider": self.provider, "identity": self.identity}
        path = self.root / "deployments" / (self.app["name"] + ".json")
        previous = path.read_bytes() if path.is_file() else None
        if previous and any(json.loads(previous).get(key) != item for key, item in value.items()):
            raise TerraformError(f"{self.app['name']} already has a deployment in another location or runtime; destroy it before moving it")
        if self.store:
            previous = self.store.read("owners/" + self.app["name"] + ".json")
            if previous and any(json.loads(previous).get(key) != item for key, item in value.items()):
                raise TerraformError(f"{self.app['name']} already has a deployment in another location or runtime; destroy it before moving it")

    def finish_destroy(self) -> None:
        path = self.root / "deployments" / (self.app["name"] + ".json")
        if self.store:
            owner = self.store.read("owners/" + self.app["name"] + ".json")
            if owner and any(json.loads(owner).get(key) != item for key, item in
                             {"provider": self.provider, "identity": self.identity}.items()):
                raise TerraformError(f"{self.app['name']} has a deployment in another location; its state was kept")
            self.store.delete("owners/" + self.app["name"] + ".json")
            self.store.delete(self.key("app") + "/deployment.json")
            self.store.delete(self.key("app") + "/configuration.json")
            self.store.cleanup()
        else:
            (self.root / (self.key("app") + "/configuration.json")).unlink(missing_ok=True)
        path.unlink(missing_ok=True)
