"""What `pdt service install` puts on a computer: the settings, the program, the proxy, and the key.

The service runs pdt from `<data folder>/versions/<version>`: its own Python from `uv python
install`, and a copy of uv, because SYSTEM or root must not run a program from a person's own
folder. The running version copies its own packages, so an install needs no release. On Windows
the packages go into that Python itself, because the `python.exe` of a virtual environment is a
launcher that starts the real Python as a second process, and the Service Control Manager logs
event 7039 when that second process connects. Elsewhere they go into a virtual environment.

`service_env` is the environment of the service and of each child: the proxy, the system
certificates, no uv configuration from a project folder, and the caches.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import sysconfig
import urllib.parse
import urllib.request
from pathlib import Path

from pdt import __version__, config, service_access
from pdt.utils import autoidm

SETTINGS_VERSION = 1
UV_TIMEOUT_SECONDS = 900
SKIPPED_PACKAGES = ("__pycache__", "*.pth", "*.pyc")
READY_FILE = "ready.json"


class InstallError(RuntimeError):
    pass


def versions_folder() -> Path:
    return autoidm.data_folder() / "versions"


def version_folder(version: str) -> Path:
    return versions_folder() / version


def python_of(folder: Path) -> Path:
    if os.name == "nt":
        found = sorted((folder / "python").glob("cpython-*/python.exe"))
        return found[-1] if found else folder / "python" / "python.exe"
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
    folder = version_folder(settings.get("pdt_version") or __version__)
    if uv_of(folder).is_file():
        env["PATH"] = str(folder / "bin") + os.pathsep + os.environ.get("PATH", "")
    return env


def run_uv(uv: str, *args: str, env: dict[str, str]) -> None:
    try:
        proc = subprocess.run([uv, *args], env={**os.environ, **env}, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=UV_TIMEOUT_SECONDS)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise InstallError(f"{uv} did not finish: {e}") from None
    if proc.returncode != 0:
        raise InstallError(f"uv {args[0]} failed: {proc.stderr.strip()[-2000:]}")


def make_environment(folder: Path, uv: str, python_version: str, settings: dict) -> None:
    """A copy of uv, Python from uv, and, off Windows, an empty virtual environment."""
    target_uv = uv_of(folder)
    target_uv.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(uv, target_uv)
    env = {**service_env(settings), "UV_PYTHON_INSTALL_DIR": str(folder / "python")}
    run_uv(str(target_uv), "python", "install", python_version, env=env)
    if os.name == "nt":
        return
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
    return python_of(folder)
