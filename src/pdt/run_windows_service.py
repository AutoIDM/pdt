#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12,<3.15"
# dependencies = ["python-dotenv"]
# ///
"""Run a web app's server, or its sign-in proxy, as a Windows service and keep the output.

usage: uv run --script run_windows_service.py app <app dir> <logs folder> <storage url>
           <read urls json> <port> <url>
       uv run --script run_windows_service.py proxy <app dir> <logs folder> <proxy exe>
           <config path>

WinSW starts this script for the services `pdt-<app>` (mode `app`) and
`pdt-<app>-proxy` (mode `proxy`) and starts it again when it exits with an
error. Each start is one run: the script writes `<logs folder>\\<UTC start>.log`
(the proxy under `<logs folder>\\proxy\\`), each line after the UTC time it
arrived, ending with `pdt: exit N`, the same shape `run_windows_task.py`
writes, so `pdt runs` and `pdt logs` read both.

A service sees no user's environment, so the script reads the project's
`.env` files itself, the nearest first, the way `pdt run` does. Mode `app`
then sets PDT_STORAGE_URL, PDT_STORAGE_READ_URLS, PDT_PORT, PDT_URL, and
PDT_RUN_ID and runs `uv run --script run.py` inside `lifecycle.py`. Mode
`proxy` renames the PDT_AUTH_ values to the names oauth2-proxy reads
(PROXY_ENV) and runs the proxy with its config file; the secrets never
touch a file.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

if "pdt" not in sys.modules:
    _package = Path(__file__).resolve().parent
    _spec = importlib.util.spec_from_file_location("pdt", _package / "__init__.py",
                                                   submodule_search_locations=[str(_package)])
    sys.modules["pdt"] = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(sys.modules["pdt"])
from pdt.run_windows_task import run_logged

USAGE = ("usage: run_windows_service.py app <app dir> <logs folder> <storage url> "
         "<read urls json> <port> <url> | proxy <app dir> <logs folder> <proxy exe> <config path>")
PROJECT_FILE = "pdt.yml"
# The env var each PDT_AUTH_ value becomes for oauth2-proxy.
PROXY_ENV = {
    "PDT_AUTH_CLIENT_ID": "OAUTH2_PROXY_CLIENT_ID",
    "PDT_AUTH_CLIENT_SECRET": "OAUTH2_PROXY_CLIENT_SECRET",
    "PDT_AUTH_COOKIE_SECRET": "OAUTH2_PROXY_COOKIE_SECRET",
    "PDT_AUTH_GOOGLE_SERVICE_ACCOUNT_PATH": "OAUTH2_PROXY_GOOGLE_SERVICE_ACCOUNT_JSON",
    "PDT_AUTH_GOOGLE_ADMIN_EMAIL": "OAUTH2_PROXY_GOOGLE_ADMIN_EMAIL",
}
# The values that name a file; a relative one is read from the project folder.
PROXY_PATHS = ("PDT_AUTH_GOOGLE_SERVICE_ACCOUNT_PATH",)


def project_folder(app_dir: Path) -> Path:
    """The nearest folder at or above `app_dir` that holds pdt.yml, else `app_dir`."""
    for folder in (app_dir, *app_dir.parents):
        if (folder / PROJECT_FILE).is_file():
            return folder
    return app_dir


def load_env(app_dir: Path) -> dict[str, str]:
    """This process's environment plus each `.env` from the app folder up to the project
    folder; a nearer file wins, and the environment itself wins over every file."""
    from dotenv import dotenv_values
    env = dict(os.environ)
    project = project_folder(app_dir)
    folders = [app_dir, *[parent for parent in app_dir.parents if project in (parent, *parent.parents)]]
    for folder in folders:
        file = folder / ".env"
        if file.is_file():
            for name, value in dotenv_values(file).items():
                if value is not None and name not in env:
                    env[name] = value
    return env


def proxy_env(env: dict[str, str], project: Path) -> dict[str, str]:
    """`env` with each PDT_AUTH_ value under the name oauth2-proxy reads."""
    out = dict(env)
    for ours, theirs in PROXY_ENV.items():
        value = env.get(ours)
        if value is None:
            continue
        if ours in PROXY_PATHS and not Path(value).is_absolute():
            value = str(project / value)
        out[theirs] = value
    return out


def main(argv: list[str]) -> int:
    if not argv or (argv[0], len(argv)) not in (("app", 7), ("proxy", 5)):
        sys.stderr.write(USAGE + "\n")
        return 2
    mode, app_dir, logs = argv[0], Path(argv[1]), Path(argv[2])
    stamp = f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
    env = load_env(app_dir)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUNBUFFERED", "1")
    uv = os.environ.get("UV") or "uv"
    if mode == "app":
        storage_url, read_urls, port, url = argv[3:]
        env.update({"PDT_STORAGE_URL": storage_url, "PDT_STORAGE_READ_URLS": read_urls,
                    "PDT_PORT": port, "PDT_URL": url, "PDT_RUN_ID": stamp})
        command = [uv, "run", "--script", str(Path(__file__).with_name("lifecycle.py")), "--",
                   uv, "run", "--script", "run.py"]
    else:
        exe, config_path = argv[3:]
        env = proxy_env(env, project_folder(app_dir))
        logs = logs / "proxy"
        command = [exe, "--config", config_path]
    logs.mkdir(parents=True, exist_ok=True)
    return run_logged(command, str(app_dir), env, logs / f"{stamp}.log")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
