"""What `pdt service install` puts on a computer: the settings, the program, the proxy, and the key.

Each version of pdt lives in `<data folder>/versions/<version>`: a virtual environment with its
own Python from `uv python install`, and a copy of uv, because SYSTEM or root must not run a
program from a person's own folder. The running version copies its own packages, so an install
needs no release of it. Another version, for a project's pin or an upgrade, comes from a signed
release: `pdt-cli-<version>.manifest.json` lists each file's hash, its `.sig` is the Ed25519
signature, `upgrade.RELEASE_PUBLIC_KEY` checks it, and uv installs from the release's locked
requirements with `--require-hashes`.

`service_env` is the environment of the service and of each child: the proxy, the system
certificates, no uv configuration from a project folder, the SSH key, and the caches.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import sysconfig
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from pdt import __version__, config, service_access, upgrade
from pdt.utils import autoidm

SETTINGS_VERSION = 1
UV_TIMEOUT_SECONDS = 900
DOWNLOAD_TIMEOUT_SECONDS = 120
SKIPPED_PACKAGES = ("__pycache__", "*.pth", "*.pyc")
RELEASE_FILES_URL = "https://github.com/AutoIDM/pdt/releases/download/v{version}/{name}"
READY_FILE = "ready.json"


class InstallError(RuntimeError):
    pass


def versions_folder() -> Path:
    return autoidm.data_folder() / "versions"


def version_folder(version: str) -> Path:
    return versions_folder() / version


def python_of(folder: Path) -> Path:
    if os.name == "nt":
        return folder / "venv" / "Scripts" / "python.exe"
    return folder / "venv" / "bin" / "python"


def uv_of(folder: Path) -> Path:
    return folder / "bin" / ("uv.exe" if os.name == "nt" else "uv")


def settings_path() -> Path:
    return autoidm.data_folder() / "service" / "settings.json"


def read_settings() -> dict | None:
    """The settings, or None with no file or while another process replaces it. A version pdt does
    not know raises InstallError that names the file."""
    try:
        found = json.loads(settings_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(found, dict):
        return None
    if found.get("version") != SETTINGS_VERSION:
        raise InstallError(f"{settings_path()} has the version {found.get('version')!r}, and this "
                           f"pdt reads version {SETTINGS_VERSION}. Upgrade pdt, or delete the file "
                           "and run pdt service install.")
    return found


def write_settings(settings: dict) -> None:
    settings_path().parent.mkdir(parents=True, exist_ok=True)
    config.write_text_atomically(settings_path(), json.dumps(settings, indent=2) + "\n")


def split_proxy_password(url: str) -> tuple[str, str | None]:
    """`url` with no password, and the password."""
    parts = urllib.parse.urlsplit(url)
    if not parts.password:
        return url, None
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    login = f"{parts.username}:***@" if parts.username else ""
    return urllib.parse.urlunsplit(parts._replace(netloc=login + host)), parts.password


def proxy_password() -> str | None:
    try:
        settings = read_settings() or {}
    except InstallError:
        return None
    proxy = settings.get("proxy") or {}
    return split_proxy_password(proxy.get("https") or proxy.get("http") or "")[1]


def find_proxy(wanted: str | None) -> dict[str, str]:
    """The proxy of the settings: `--proxy`, else the system setting. SYSTEM and root may have no
    proxy of their own, so the install stores what the person's terminal sees."""
    if wanted:
        return {"http": wanted, "https": wanted, "no": os.environ.get("NO_PROXY") or ""}
    found = urllib.request.getproxies()
    if not (found.get("https") or found.get("http")):
        return {}
    no_proxy = found.get("no") or os.environ.get("NO_PROXY") or ""
    return {"http": found.get("http") or found.get("https"),
            "https": found.get("https") or found.get("http"), "no": no_proxy}


def ssh_command(settings: dict) -> str | None:
    ssh = settings.get("ssh")
    if not ssh:
        return None
    return (f"ssh -i {ssh['key']} -o UserKnownHostsFile={ssh['known_hosts']} "
            "-o StrictHostKeyChecking=yes -o IdentitiesOnly=yes")


def service_env(settings: dict) -> dict[str, str]:
    """The env values the service sets for itself and for each child."""
    data = autoidm.data_folder()
    env = {"UV_SYSTEM_CERTS": "1", "UV_NO_CONFIG": "1",
           "UV_CACHE_DIR": str(data / "cache" / "uv"),
           "UV_PYTHON_INSTALL_DIR": str(data / "cache" / "python"),
           "UV_TOOL_DIR": str(data / "cache" / "tools"),
           "XDG_CACHE_HOME": str(data / "cache")}
    proxy = settings.get("proxy") or {}
    if proxy.get("https"):
        env.update({"HTTP_PROXY": proxy["http"], "HTTPS_PROXY": proxy["https"],
                    "NO_PROXY": proxy.get("no") or ""})
    command = ssh_command(settings)
    if command:
        env["GIT_SSH_COMMAND"] = command
    folder = version_folder(settings.get("pdt_version") or __version__)
    if uv_of(folder).is_file():
        env["PATH"] = str(folder / "bin") + os.pathsep + os.environ.get("PATH", "")
    return env


def store_ssh_key(key_file: str, known_hosts: str | None) -> dict[str, str]:
    """Copy the key and known_hosts to `<data folder>/ssh`. A missing file stops the command."""
    source_hosts = Path(known_hosts) if known_hosts else Path.home() / ".ssh" / "known_hosts"
    key, hosts = Path(key_file).expanduser(), source_hosts.expanduser()
    for path in (key, hosts):
        if not path.is_file():
            raise InstallError(f"The file {path} does not exist. Name it with --ssh-key or "
                               "--known-hosts, then run the command again.")
    folder = autoidm.data_folder() / "ssh"
    service_access.limit(folder)
    target_key, target_hosts = folder / "id_key", folder / "known_hosts"
    shutil.copyfile(key, target_key)
    shutil.copyfile(hosts, target_hosts)
    if os.name != "nt":
        os.chmod(target_key, 0o600)
    return {"key": str(target_key), "known_hosts": str(target_hosts)}


def run_uv(uv: str, *args: str, env: dict[str, str]) -> None:
    try:
        proc = subprocess.run([uv, *args], env={**os.environ, **env}, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=UV_TIMEOUT_SECONDS)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise InstallError(f"{uv} did not finish: {e}") from None
    if proc.returncode != 0:
        raise InstallError(f"uv {args[0]} failed: {proc.stderr.strip()[-2000:]}")


def make_environment(folder: Path, uv: str, python_version: str, settings: dict) -> None:
    """A copy of uv, Python from uv, and an empty virtual environment."""
    target_uv = uv_of(folder)
    target_uv.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(uv, target_uv)
    env = {**service_env(settings), "UV_PYTHON_INSTALL_DIR": str(folder / "python")}
    run_uv(str(target_uv), "python", "install", python_version, env=env)
    run_uv(str(target_uv), "venv", "--python", python_version, "--python-preference", "only-managed",
           "--no-project", str(folder / "venv"), env=env)


def site_packages_of(python: Path) -> Path:
    proc = subprocess.run([str(python), "-c",
                           "import sysconfig; print(sysconfig.get_path('purelib'))"],
                          stdin=subprocess.DEVNULL, capture_output=True, text=True, check=True)
    return Path(proc.stdout.strip())


def copy_running(folder: Path) -> None:
    """Copy this pdt's packages, so the new environment holds the same bytes. A clone's pdt is in
    `src`, and only a `.pth` file names it, so it goes in by name."""
    target = site_packages_of(python_of(folder))
    source = Path(sysconfig.get_path("purelib"))
    shutil.copytree(source, target, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns(*SKIPPED_PACKAGES))
    package = Path(__file__).resolve().parent
    if package.parent != source:
        shutil.copytree(package, target / "pdt", dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("__pycache__"))


def find_uv() -> str:
    found = shutil.which("uv")
    if found is None:
        raise InstallError("pdt did not find uv on the PATH, so it cannot install its own Python.")
    return found


def install_running(settings: dict) -> Path:
    """The Python of this version in the data folder, installed once."""
    folder = version_folder(__version__)
    python = python_of(folder)
    if (folder / READY_FILE).is_file() and python.is_file():
        return python
    if folder.exists():
        shutil.rmtree(folder)
    service_access.limit(versions_folder(), private=False)
    folder.mkdir(parents=True)
    make_environment(folder, find_uv(), f"{sys.version_info.major}.{sys.version_info.minor}",
                     settings)
    copy_running(folder)
    (folder / READY_FILE).write_text(json.dumps({"version": __version__}) + "\n", encoding="utf-8")
    return python


def download(url: str) -> bytes:
    try:
        with urllib.request.urlopen(url, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response:
            return response.read()
    except (urllib.error.URLError, OSError) as e:
        raise InstallError(f"pdt could not download {url}: {getattr(e, 'reason', e)}") from None


def release_files(version: str, work: Path) -> dict[str, Path]:
    """Download the files of a release to `work`, and check the signature and each hash."""
    public = upgrade.RELEASE_PUBLIC_KEY
    if not public:
        raise InstallError("This build of pdt has no release key, so it installs no other "
                           f"version than {__version__}.")
    name = f"pdt-cli-{version}.manifest.json"
    manifest = download(RELEASE_FILES_URL.format(version=version, name=name))
    signature = download(RELEASE_FILES_URL.format(version=version, name=f"{name}.sig"))
    try:
        key = Ed25519PublicKey.from_public_bytes(base64.urlsafe_b64decode(public + "=" * (-len(public) % 4)))
        key.verify(base64.urlsafe_b64decode(signature.strip() + b"=" * (-len(signature.strip()) % 4)),
                   manifest)
    except (InvalidSignature, ValueError) as e:
        raise InstallError(f"The signature of {name} is not valid: {e}") from None
    found = json.loads(manifest)
    if found.get("version") != version or not isinstance(found.get("files"), dict):
        raise InstallError(f"{name} does not describe version {version}.")
    paths = {}
    for file_name, digest in found["files"].items():
        if Path(file_name).name != file_name:
            raise InstallError(f"{name} names the file {file_name}, which is not a plain name.")
        data = download(RELEASE_FILES_URL.format(version=version, name=file_name))
        if hashlib.sha256(data).hexdigest() != digest:
            raise InstallError(f"The hash of {file_name} differs from {name}.")
        paths[file_name] = work / file_name
        paths[file_name].write_bytes(data)
    return paths


def install_release(version: str, settings: dict) -> Path:
    """The Python of another version in the data folder, from its signed release."""
    folder = version_folder(version)
    python = python_of(folder)
    if (folder / READY_FILE).is_file() and python.is_file():
        return python
    if folder.exists():
        shutil.rmtree(folder)
    service_access.limit(versions_folder(), private=False)
    folder.mkdir(parents=True)
    make_environment(folder, find_uv(), f"{sys.version_info.major}.{sys.version_info.minor}",
                     settings)
    work = folder / "release"
    work.mkdir()
    paths = release_files(version, work)
    wheel = next((path for name, path in paths.items() if name.endswith(".whl")), None)
    lock = next((path for name, path in paths.items() if name.endswith(".lock.txt")), None)
    if wheel is None or lock is None:
        raise InstallError(f"The release of version {version} holds no wheel or no lock file.")
    uv = str(uv_of(folder))
    env = service_env(settings)
    run_uv(uv, "pip", "install", "--python", str(python), "--require-hashes", "-r", str(lock), env=env)
    run_uv(uv, "pip", "install", "--python", str(python), "--no-deps", str(wheel), env=env)
    shutil.rmtree(work)
    (folder / READY_FILE).write_text(json.dumps({"version": version}) + "\n", encoding="utf-8")
    return python


def python_for(version: str, settings: dict) -> Path:
    return install_running(settings) if version == __version__ else install_release(version, settings)
