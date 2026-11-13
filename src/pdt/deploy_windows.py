#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12,<3.15"
# dependencies = [
#     "pyyaml",
#     "rich",
#     "python-dotenv",
#     "backoff",
#     "fsspec",
#     "duckdb",
# ]
# ///
"""Deploy an app to this Windows PC.

Per app on this PC there is one folder, `%ProgramData%\\pdt\\<app>\\`, with
`storage\\` (the app's files, kept after destroy) and `logs\\` (one log per
run, removed on destroy). What else the app gets depends on its profile:

- a `job` is a scheduled task `pdt-<app>`; `deploy_windows_job.py` holds it.
- a `web` app is two Windows services, `pdt-<app>` and `pdt-<app>-proxy`;
  `deploy_windows_web.py` holds them.

This module keeps what both share: the folders, the PowerShell runner (an
elevated script gets one UAC prompt), the `Managed by pdt;` check on a task,
and `main()`, which reads the app's `app_profile` and hands every command to
the profile's module.
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import importlib.util
import os
import shutil
import subprocess
import sys
import time  # noqa: F401 - tests patch the clock through this module
from pathlib import Path

if "pdt" not in sys.modules:
    _package = Path(__file__).resolve().parent
    _spec = importlib.util.spec_from_file_location("pdt", _package / "__init__.py",
                                                   submodule_search_locations=[str(_package)])
    sys.modules["pdt"] = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(sys.modules["pdt"])
from pdt import config, console, regions, runs_cli
from pdt import powershell as ps
from pdt.deploy import DECLINED, confirm, show_plan  # noqa: F401 - the profile modules use them
from pdt.deploy_common import CostEstimate, run_main, warn_if_locked  # noqa: F401
from pdt.pwsh import ensure_pwsh
from pdt.run_windows_task import EVENT_SOURCE, LINE_TIME, MANUAL_START  # noqa: F401


class WindowsDeployError(Exception):
    pass


FORBIDDEN_TASK_NAME_CHARS = set('\\/:*?"<>|')


def app_folder(app_name: str) -> Path:
    """The app's folder under %ProgramData%\\pdt; the task name is unique per PC, so the
    app name is too."""
    return config.machine_data_home() / app_name


def storage_folder(app_name: str) -> Path:
    return app_folder(app_name) / "storage"


def logs_folder(app_name: str) -> Path:
    return app_folder(app_name) / "logs"


def _task_name(app_name: str) -> str:
    """`pdt-<app>`: the name of the app's task, or of its services and firewall rule."""
    name = f"pdt-{app_name}"
    if any(char in FORBIDDEN_TASK_NAME_CHARS or ord(char) < 32 for char in name):
        raise WindowsDeployError(
            f"app name {app_name!r} contains characters Windows forbids in task names")
    return name


def storage_url(app_name: str) -> str:
    """The `file:` URI `pdt.utils.storage.root()` reads from PDT_STORAGE_URL."""
    return storage_folder(app_name).as_uri() + "/"


def uv_on_machine_path() -> bool:
    """Whether the machine PATH, the only PATH the SYSTEM account sees, holds uv.exe."""
    if os.name != "nt":
        return False
    import winreg
    try:
        with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment") as key:
            path, _type = winreg.QueryValueEx(key, "Path")
    except OSError:
        return False
    return any((Path(folder) / "uv.exe").is_file()
               for folder in os.path.expandvars(path).split(";") if folder)


def _preflight(require_uv: bool = True) -> tuple[str, str | None]:
    if sys.platform != "win32":
        raise WindowsDeployError(
            f"provider 'windows' requires Windows; current platform is {sys.platform}")
    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    if powershell is None:
        raise WindowsDeployError("Windows PowerShell was not found on PATH")
    uv = shutil.which("uv.exe") or shutil.which("uv")
    if require_uv and uv is None:
        raise WindowsDeployError(
            "uv is unavailable; run this deployment through pdt.bat")
    return powershell, uv


def _encoded(script: str) -> str:
    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")


def _is_admin() -> bool:
    return bool(ctypes.windll.shell32.IsUserAnAdmin())


def _run(powershell: str, script: str, *, not_found_ok: bool = False,
         elevate: bool = False) -> bool:
    if elevate and not _is_admin():
        script = (
            "$p = Start-Process powershell -Verb RunAs -Wait -PassThru "
            "-WindowStyle Hidden -ArgumentList '-NoLogo','-NoProfile',"
            "'-NonInteractive','-ExecutionPolicy','Bypass',"
            f"'-EncodedCommand','{_encoded(script)}'; exit $p.ExitCode"
        )
    proc = subprocess.run(
        [powershell, "-NoLogo", "-NoProfile", "-NonInteractive",
         "-ExecutionPolicy", "Bypass", "-EncodedCommand", _encoded(script)],
        stdin=subprocess.DEVNULL, capture_output=True, text=True)
    if proc.returncode == 0:
        return True
    if not_found_ok and proc.returncode == 3:
        return False
    detail = (proc.stderr or proc.stdout).strip()
    raise WindowsDeployError(
        f"Windows Task Scheduler command failed"
        + (f": {detail}" if detail else f" (exit {proc.returncode})"))


def _run_output(powershell: str, script: str) -> str:
    proc = subprocess.run(
        [powershell, "-NoLogo", "-NoProfile", "-NonInteractive",
         "-ExecutionPolicy", "Bypass", "-EncodedCommand", _encoded(script)],
        stdin=subprocess.DEVNULL, capture_output=True, text=True)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        raise WindowsDeployError(
            "PowerShell command failed"
            + (f": {detail}" if detail else f" (exit {proc.returncode})"))
    return proc.stdout


def _ps_string(value: str) -> str:
    return ps.quoted(value)


def _deploying_user() -> str:
    """The account running deploy, as DOMAIN\\user. Read before elevation, because the
    UAC prompt may switch to an administrator's account when this user is not one."""
    proc = subprocess.run(["whoami"], stdin=subprocess.DEVNULL, capture_output=True, text=True)
    user = proc.stdout.strip()
    if proc.returncode != 0 or user == "":
        raise WindowsDeployError("whoami could not name the current Windows user")
    return user


def _folder_script(app: dict, user: str) -> str:
    """Create the app folder with the two rules its files need: SYSTEM (the task) has full
    control, and the deploying user can change and delete what SYSTEM writes."""
    folders = [app_folder(app["name"]), logs_folder(app["name"])]
    if app["storage"]:
        folders.append(storage_folder(app["name"]))
    paths = ", ".join(_ps_string(str(folder)) for folder in folders)
    return (
        f"$folder = {_ps_string(str(app_folder(app['name'])))}; "
        f"New-Item -ItemType Directory -Force -Path {paths} | Out-Null; "
        f"icacls $folder /grant '*S-1-5-18:(OI)(CI)F' {_ps_string(user + ':(OI)(CI)M')} "
        "| Out-Null; "
        "if ($LASTEXITCODE -ne 0) { throw \"icacls failed with exit $LASTEXITCODE\" }; "
    )


def _task_state(powershell: str, name: str) -> str:
    quoted = _ps_string(name)
    script = (
        f"$name = {quoted}; $task = Get-ScheduledTask -TaskName $name "
        "-TaskPath '\\' -ErrorAction SilentlyContinue; "
        "if ($null -eq $task) { exit 3 }; "
        "if (-not $task.Description.StartsWith('Managed by pdt;')) { exit 4 }"
    )
    proc = subprocess.run(
        [powershell, "-NoLogo", "-NoProfile", "-NonInteractive",
         "-ExecutionPolicy", "Bypass", "-EncodedCommand", _encoded(script)],
        stdin=subprocess.DEVNULL, capture_output=True, text=True)
    if proc.returncode == 0:
        return "managed"
    if proc.returncode == 3:
        return "absent"
    if proc.returncode == 4:
        return "unmanaged"
    detail = (proc.stderr or proc.stdout).strip()
    raise WindowsDeployError(
        "Windows Task Scheduler command failed"
        + (f": {detail}" if detail else f" (exit {proc.returncode})"))


def _task_running(powershell: str, name: str) -> bool:
    script = (
        f"$task = Get-ScheduledTask -TaskName {_ps_string(name)} -TaskPath '\\' "
        "-ErrorAction SilentlyContinue; "
        "if ($null -ne $task -and $task.State -eq 'Running') { exit 0 }; exit 1"
    )
    proc = subprocess.run(
        [powershell, "-NoLogo", "-NoProfile", "-NonInteractive",
         "-ExecutionPolicy", "Bypass", "-EncodedCommand", _encoded(script)],
        stdin=subprocess.DEVNULL, capture_output=True, text=True)
    return proc.returncode == 0


def _missing_host_modules(pwsh: str, names: list[str]) -> list[str]:
    """The modules Windows itself provides (an optional feature) that this PC lacks."""
    if not names:
        return []
    listed = ",".join(map(_ps_string, names))
    script = (f"$have = Get-Module -ListAvailable -Name {listed} | "
              "Select-Object -ExpandProperty Name; "
              f"{listed} | Where-Object {{ $have -notcontains $_ }}")
    return _run_output(pwsh, script).split()


def _host_module_problem(name: str) -> str:
    feature = ps.WINDOWS_IN_BOX_MODULES.get(name.lower())
    if feature is None:
        return (f"the scripts use the PowerShell module {name}, which ships with Windows, "
                "and this Windows edition lacks it; remove it from the scripts")
    return (f"the scripts use the PowerShell module {name}, which this PC does not have; "
            f"add the Windows feature \"{feature}\" in Settings > System > Optional "
            "features, then deploy again")


def _prepare_powershell(app: dict) -> tuple[str | None, list]:
    """The pwsh that runs a PowerShell app and the gallery modules to install; (None, [])
    for a Python app."""
    if not config.powershell_scripts(app["dir"]):
        return None, []
    pwsh = ensure_pwsh()
    scan = ps.scan(app, "windows")
    missing = _missing_host_modules(pwsh, scan.host_modules)
    if missing:
        raise WindowsDeployError("; ".join(map(_host_module_problem, missing)))
    return pwsh, scan.modules


def _kept_storage_line(app_name: str) -> str:
    folder = storage_folder(app_name)
    count = sum(1 for file in folder.rglob("*") if file.is_file()) if folder.is_dir() else 0
    return f"kept: folder {console.value(folder)} ({count} files)"


def _store(app_name: str):
    from pdt.utils.storage import Store

    return Store(storage_url(app_name), None)


def storage(app: dict, rest: list[str], assume_yes: bool) -> int:
    from pdt import storage_cli

    return storage_cli.run(_store(app["name"]), app, rest, assume_yes,
                           str(storage_folder(app["name"])))


# The job's functions by their old names, for the code and tests that import them from here.
from pdt.deploy_windows_job import (  # noqa: E402
    EXIT_LINE_BYTES, FOLLOW_POLL_SECONDS, RUN_TIME_LIMIT, RUNNER, deploy, destroy, list_runs,
    logs, parse_line, pause, plan, read_lines, schedule_trigger, start, task_arguments, task_xml,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command",
                        choices=("deploy", "destroy", "login", "storage", "secrets",
                                 "runs", "logs", "pause", "unpause", "start"))
    parser.add_argument("app")
    parser.add_argument("rest", nargs="*")
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--plan", action="store_true")
    args = parser.parse_intermixed_args()
    try:
        app = config.merged_app(args.app)
    except config.ConfigError as exc:
        console.error(console.escape(str(exc)))
        return 1
    if sys.platform != "win32":
        console.error(
            "the windows provider targets this computer, but the current operating system "
            "is not Windows. Run this command on the Windows PC.")
        return 1
    if args.command == "login":
        console.note("the windows provider deploys to this computer, so it needs no login.")
        return 0
    if args.command == "secrets":
        console.note("the windows provider reads your .env file at every run, "
                     "so there is nothing to compare, save, get, or set.")
        return 0
    config.load_env(app["dir"])
    if args.command == "storage":
        return storage(app, args.rest, args.yes)
    if app["app_profile"] == "web":
        from pdt import deploy_windows_web
        return deploy_windows_web.command(app, args.command, args.rest, args.yes, args.plan)
    from pdt import deploy_windows_job
    return deploy_windows_job.command(app, args.command, args.rest, args.yes, args.plan)


if __name__ == "__main__":
    console.json_output()
    run_main(main)
