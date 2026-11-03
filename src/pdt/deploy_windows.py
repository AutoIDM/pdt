#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "pyyaml",
#     "rich",
#     "python-dotenv",
#     "backoff",
#     "fsspec",
#     "duckdb",
#     "pypsrp>=0.9.1; sys_platform != 'win32' or platform_machine != 'ARM64'",
#     "pyspnego[kerberos]; sys_platform == 'darwin'",
# ]
# ///
"""Deploy an app as a Windows Task Scheduler task on this PC.

A PowerShell app (.ps1 files, no run.py) runs through run_powershell.py
instead of run.py, unless `pdt new APP --from-scripts` wrote it one. Before
the plan, deploy finds pwsh (`pdt.pwsh` installs the pinned version under
%ProgramData%\\pdt\\pwsh when none is on the PATH), scans the scripts, and
stops with the Windows feature to add when a module that comes with Windows
(`powershell.WINDOWS_IN_BOX_MODULES`) is missing. The
elevated script installs the other modules for all users, so SYSTEM sees them.
"""

from __future__ import annotations

import argparse
import base64
import csv
import ctypes
import html
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterator

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pdt import config, console, regions, runs_cli, windows_remote, windows_uv
from pdt.windows_remote import SET_ACL, replace_file, set_acl
from pdt import powershell as ps
from pdt.deploy import DECLINED, confirm, show_plan
from pdt.deploy_common import (CostEstimate, gather_secrets, run_main, run_secrets,
                               secret_contents, stage_build_context, warn_if_locked)
from pdt.pwsh import ensure_pwsh
from pdt.run_windows_task import EVENT_SOURCE, LINE_TIME, MANUAL_START, SECRETS_FILE


class WindowsDeployError(Exception):
    pass


MONTHS = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)
DAYS = ("Sunday", "Monday", "Tuesday", "Wednesday",
        "Thursday", "Friday", "Saturday")
FORBIDDEN_TASK_NAME_CHARS = set('\\/:*?"<>|')
NO_RUN_TIME_LIMIT = "PT0S"
# A repeat ends 30 seconds after the last start of the day, so the repeat at the start of the
# next day, which Windows counts as inside a 24-hour duration, never runs on a weekly or
# monthly trigger.
MINUTES_A_DAY = 24 * 60
FOLLOW_POLL_SECONDS = 0.25
TASK_FOLDER_NAME = "pdt"
TASK_FOLDER = f"\\{TASK_FOLDER_NAME}\\"
MANAGED = "Managed by pdt;"
# Columns of `schtasks /Query /V /FO CSV`. Most columns hold text in the PC's language;
# these two do not.
LAST_RESULT_COLUMN = 6
COMMENT_COLUMN = 10
SCHED_S_TASK_RUNNING = 0x41301
SCHED_S_TASK_HAS_NOT_RUN = 0x41303
LOG_START_SLACK = timedelta(seconds=2)
LOG_START_WINDOW = timedelta(minutes=15)
EXIT_LINE_BYTES = 256


def schedule_triggers(cron: str) -> str:
    problem = config.windows_schedule_problem(cron)
    if problem != "":
        raise WindowsDeployError(problem)
    kind, values, months = config.windows_trigger_days(cron)
    if kind == "daily":
        days = "<ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay>"
    elif kind == "weekly":
        day_xml = "".join(f"<{DAYS[value]}/>" for value in values)
        days = (f"<ScheduleByWeek><WeeksInterval>1</WeeksInterval><DaysOfWeek>{day_xml}"
                "</DaysOfWeek></ScheduleByWeek>")
    else:
        days_xml = "".join(f"<Day>{value}</Day>" for value in values)
        months_xml = "".join(f"<{MONTHS[value - 1]}/>" for value in months)
        days = (f"<ScheduleByMonth><DaysOfMonth>{days_xml}</DaysOfMonth><Months>{months_xml}"
                "</Months></ScheduleByMonth>")
    starts, every = config.windows_trigger_times(cron)
    repetition = ""
    if every:
        interval = "PT1H" if every == 60 else f"PT{every}M"
        repetition = (f"<Repetition><Interval>{interval}</Interval>"
                      f"<Duration>PT{MINUTES_A_DAY - every}M30S</Duration>"
                      "<StopAtDurationEnd>false</StopAtDurationEnd></Repetition>")
    return "".join(
        f"<CalendarTrigger>{repetition}<StartBoundary>2000-01-01T{hour:02d}:{minute:02d}:00"
        f"</StartBoundary><ExecutionTimeLimit>{NO_RUN_TIME_LIMIT}</ExecutionTimeLimit>"
        f"<Enabled>true</Enabled>{days}</CalendarTrigger>"
        for hour, minute in starts)


def app_folder(app_name: str) -> Path:
    """The app's folder under %ProgramData%\\pdt; the task name is unique per PC, so the
    app name is too."""
    return config.machine_data_home() / app_name


def storage_folder(app_name: str) -> Path:
    return app_folder(app_name) / "storage"


def logs_folder(app_name: str) -> Path:
    return app_folder(app_name) / "logs"


def _task_name(app_name: str) -> str:
    name = f"pdt-{app_name}"
    if any(char in FORBIDDEN_TASK_NAME_CHARS or ord(char) < 32 for char in name):
        raise WindowsDeployError(
            f"app name {app_name!r} contains characters Windows forbids in task names")
    return name


def _task_path(app_name: str) -> str:
    return TASK_FOLDER + _task_name(app_name)


def _root_folder_path(app_name: str) -> str:
    return "\\" + _task_name(app_name)


def _task_sddl(sid: str) -> str:
    """Administrators and SYSTEM own the task; the deploying user may read, start, enable,
    and disable it with no UAC prompt. Task Scheduler still refuses that user a change to
    the task's action or account, and a delete."""
    return f"D:(A;;FA;;;BA)(A;;FA;;;SY)(A;;FRFWFX;;;{sid})"


def storage_url(app_name: str) -> str:
    """The `file:` URI `pdt.utils.storage.root()` reads from PDT_STORAGE_URL."""
    return storage_folder(app_name).as_uri() + "/"


def deployed_folder(app_name: str) -> Path:
    return app_folder(app_name) / "app"


def runtime_folder(app_name: str) -> Path:
    return app_folder(app_name) / "runtime"


def secrets_file(app_name: str) -> Path:
    return app_folder(app_name) / SECRETS_FILE


def deployed_app_dir(app_name: str) -> Path:
    return deployed_folder(app_name) / app_name


def machine_uv() -> Path:
    return Path(windows_uv.machine_uv(str(config.machine_data_home().parent)))


def machine_cache() -> Path:
    return Path(windows_uv.machine_cache(str(config.machine_data_home().parent)))


def task_arguments(app: dict) -> str:
    name = app["name"]
    return subprocess.list2cmdline([
        "--cache-dir", str(machine_cache()),
        "run", "--python", windows_uv.PYTHON, "--script", str(runtime_folder(name) / "pdt" / "run_windows_task.py"),
        str(deployed_app_dir(name)), str(logs_folder(name)), storage_url(name)])


def task_xml(app: dict) -> tuple[str, str]:
    cron = config.cron_expression(app["schedule"])
    description = config.describe_schedule(cron)
    trigger = schedule_triggers(cron)
    command = html.escape(str(machine_uv()))
    arguments = html.escape(task_arguments(app))
    workdir = html.escape(str(deployed_app_dir(app["name"])))
    task_description = html.escape(
        f"{MANAGED} runs {app['name']} from {deployed_app_dir(app['name'])}")
    xml = f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>{task_description}</Description></RegistrationInfo>
  <Triggers>{trigger}</Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>S-1-5-18</UserId>
      <RunLevel>HighestAvailable</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <Enabled>{"false" if app.get("pause") else "true"}</Enabled>
    <Hidden>false</Hidden>
    <ExecutionTimeLimit>{NO_RUN_TIME_LIMIT}</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{command}</Command>
      <Arguments>{arguments}</Arguments>
      <WorkingDirectory>{workdir}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>"""
    return description, xml


def _preflight() -> str:
    if sys.platform != "win32":
        raise WindowsDeployError(
            f"provider 'windows' requires Windows; current platform is {sys.platform}")
    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    if powershell is None:
        raise WindowsDeployError("Windows PowerShell was not found on PATH")
    return powershell


def _encoded(script: str) -> str:
    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")


def _is_admin() -> bool:
    return bool(ctypes.windll.shell32.IsUserAnAdmin())


def run_args(powershell: str, script_file: Path, elevate: bool) -> list[str]:
    """Run `script_file`, through one UAC prompt when `elevate`. A file, not -EncodedCommand,
    because a deploy script encoded twice passes the 32767-character command line limit."""
    run = ["-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass"]
    if not elevate:
        return [powershell, *run, "-File", str(script_file)]
    arguments = " ".join(run) + f' -File "{script_file}"'
    wrapper = (f"$p = Start-Process powershell -Verb RunAs -Wait -PassThru -WindowStyle Hidden "
               f"-ArgumentList {_ps_string(arguments)}; exit $p.ExitCode")
    return [powershell, *run, "-EncodedCommand", _encoded(wrapper)]


def _run(powershell: str, script: str, *, not_found_ok: bool = False,
         elevate: bool = False) -> bool:
    folder = Path(tempfile.mkdtemp(prefix="pdt-step-"))
    try:
        script_file = folder / "step.ps1"
        script_file.write_text(script, encoding="utf-8-sig")
        proc = subprocess.run(run_args(powershell, script_file, elevate and not _is_admin()),
                              stdin=subprocess.DEVNULL, capture_output=True, text=True)
    finally:
        shutil.rmtree(folder, ignore_errors=True)
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


def _deploying_user() -> tuple[str, str]:
    """The account running deploy, as DOMAIN\\user, and its SID. Read before elevation,
    because the UAC prompt may switch to an administrator's account when this user is not one."""
    proc = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"], stdin=subprocess.DEVNULL,
                          capture_output=True, text=True, errors="replace")
    row = next(csv.reader(proc.stdout.splitlines()), [])
    if proc.returncode != 0 or len(row) != 2:
        raise WindowsDeployError("whoami could not name the current Windows user")
    return row[0], row[1]


def _folder_script(app: dict, user: str) -> str:
    """Create the app folder: SYSTEM and Administrators own it and Users can read it, because
    SYSTEM runs what it holds; the deploying user can change and delete what the task writes
    in logs and storage."""
    folders = [logs_folder(app["name"])]
    if app["storage"]:
        folders.append(storage_folder(app["name"]))
    paths = ", ".join(_ps_string(str(folder)) for folder in folders)
    grant = _ps_string(user + ":(OI)(CI)M")
    return (
        f"$folder = {_ps_string(str(app_folder(app['name'])))}; "
        "New-Item -ItemType Directory -Force -Path $folder | Out-Null; "
        f"{set_acl('$folder', True)}"
        f"foreach ($sub in @({paths})) {{ New-Item -ItemType Directory -Force -Path $sub | Out-Null; "
        f"icacls $sub /grant {grant} | Out-Null; "
        "if ($LASTEXITCODE -ne 0) { throw \"icacls failed with exit $LASTEXITCODE\" } }; "
    )


def _schtasks(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["schtasks.exe", *args], stdin=subprocess.DEVNULL,
                          capture_output=True, text=True, errors="replace")


def _task_state(path: str) -> str:
    """A task this account may not read is absent to it, as Task Scheduler shows it."""
    proc = _schtasks("/Query", "/TN", path, "/V", "/FO", "CSV", "/NH")
    row = next(csv.reader(proc.stdout.splitlines()), [])
    if proc.returncode != 0 or len(row) <= COMMENT_COLUMN:
        return "absent"
    if not row[COMMENT_COLUMN].startswith(MANAGED):
        return "unmanaged"
    return "running" if row[LAST_RESULT_COLUMN] == str(SCHED_S_TASK_RUNNING) else "ready"


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


def plan(app: dict, verb: str, description: str, user: str, secrets: dict[str, str],
         install_uv: bool, pwsh: str | None = None, modules: list = (),
         legacy: bool = False) -> list[console.Markup]:
    name = app["name"]
    actions = [
        console.Markup(f"{verb} Windows scheduled task {console.value(_task_path(name))} "
                       "(runs as SYSTEM)"),
        console.Markup(f"run {console.value(name)} {console.escape(description)} "
                       "(machine local time)" + (" (paused)" if app.get("pause") else "")),
        console.Markup("runs only while this PC is on; a missed run starts when the PC is "
                       "next on"),
        console.Markup(f"let {console.value(user)} start, pause, and unpause the task with no "
                       "administrator prompt"),
        console.Markup(f"copy the app to {console.value(deployed_folder(name))} and pdt "
                       f"{console.value(windows_remote.pdt_version())} to {console.value(runtime_folder(name))}; "
                       "the task runs this copy, so a change to the project reaches the job "
                       "at the next deploy"),
        console.Markup(f"store {secret_contents(secrets)} in {console.value(secrets_file(name))} "
                       "(SYSTEM and Administrators only)"),
        console.Markup(f"install uv {console.value(windows_uv.VERSION)} at "
                       f"{console.value(machine_uv())}" if install_uv else
                       f"run uv {console.value(windows_uv.VERSION)} from "
                       f"{console.value(machine_uv())}"),
        console.Markup(f"keep the app's run data in {console.value(app_folder(name))} "
                       f"(SYSTEM and Administrators: full control; {console.value(user)}: "
                       "modify in logs and storage)"),
        console.Markup(f"write one log per run under {console.value(logs_folder(name))} "
                       "(removed on destroy)"),
        console.Markup("write each run's result to the Windows Application log, source "
                       f"{console.value(EVENT_SOURCE)}"),
    ]
    dockerfile = Path(app["dir"]) / "Dockerfile"
    if dockerfile.is_file():
        actions.append(console.Markup(
            f"ignore {console.value(dockerfile)} "
            f"({console.escape(config.dockerfile_problem(app['platform']))})"))
    if app["storage"]:
        actions.append(console.Markup(f"use folder {console.value(storage_folder(name))} for "
                                      "the app's files (kept after destroy)"))
    if pwsh is not None:
        actions.append(console.Markup(f"run the app's .ps1 files with PowerShell from "
                                      f"{console.value(pwsh)}"))
    if modules:
        names = ", ".join(console.value(need.name) for need in modules)
        actions.append(console.Markup(f"install PowerShell modules {names} (all users)"))
    if legacy:
        actions.append(console.Markup(f"delete the task {console.value(_root_folder_path(name))} that "
                                      "an earlier pdt made in the root task folder"))
    return actions


def _machine_uv_current() -> bool:
    try:
        proc = subprocess.run([str(machine_uv()), "--version"], stdin=subprocess.DEVNULL,
                              capture_output=True, text=True)
    except OSError:
        return False
    return proc.returncode == 0 and proc.stdout.split()[1:2] == [windows_uv.VERSION]


def stage_release(app: dict, secrets: dict[str, str], uv: Path | None) -> Path:
    context = stage_build_context(app)
    release = Path(tempfile.mkdtemp(prefix="pdt-release-"))
    shutil.rmtree(context / ".pdt-lifecycle", ignore_errors=True)
    shutil.move(str(context), str(release / "app"))
    shutil.copytree(Path(__file__).resolve().parent, release / "runtime" / "pdt",
                    ignore=shutil.ignore_patterns("__pycache__"))
    (release / SECRETS_FILE).write_text(json.dumps(secrets, sort_keys=True), encoding="utf-8")
    if uv is not None:
        shutil.copy(uv, release / "uv.exe")
    return release


def _release_script(app: dict, release: Path, install: str, register: str) -> str:
    """Copy the release next to the deployed one (`app.new`, `runtime.new`), install the
    modules, then swap both folders and the secrets file in and register the task. A failure
    at any step puts the last release back, so a failed deploy leaves it whole."""
    name = app["name"]
    source = _ps_string(str(release))
    targets = ", ".join(_ps_string(str(folder))
                        for folder in (deployed_folder(name), runtime_folder(name)))
    stage = ""
    for part, folder in (("app", deployed_folder(name)), ("runtime", runtime_folder(name))):
        stage += (f"$new = {_ps_string(str(folder) + '.new')}; "
                  "if (Test-Path $new) { Remove-Item -Recurse -Force $new }; "
                  f"Copy-Item -Recurse -Path (Join-Path {source} '{part}') -Destination $new; ")
    if (release / "uv.exe").is_file():
        stage += (f"$bin = {_ps_string(str(machine_uv().parent))}; "
                  "New-Item -ItemType Directory -Force -Path $bin | Out-Null; "
                  f"{set_acl('$bin', True)}"
                  + replace_file(f"(Join-Path {source} 'uv.exe')", machine_uv(), True))
    stage += (f"$cache = {_ps_string(str(machine_cache()))}; "
              "New-Item -ItemType Directory -Force -Path $cache | Out-Null; "
              f"{set_acl('$cache', False)}"
              f"$secret = {_ps_string(str(secrets_file(name)))}; $placed = \"$secret.tmp\"; "
              "if (Test-Path $placed) { Remove-Item -Force $placed }; "
              "New-Item -ItemType File -Path $placed | Out-Null; "
              f"{set_acl('$placed', False)}"
              "[IO.File]::WriteAllBytes($placed, [IO.File]::ReadAllBytes("
              f"(Join-Path {source} '{SECRETS_FILE}'))); ")
    return (
        f"$targets = @({targets}); $swapped = @(); $oldSecret = $false; $newSecret = $false; "
        f"try {{ {stage}{install}"
        "foreach ($t in $targets) { "
        "if (Test-Path \"$t.old\") { Remove-Item -Recurse -Force \"$t.old\" }; "
        "if (Test-Path $t) { Rename-Item -Path $t -NewName ((Split-Path $t -Leaf) + '.old') }; "
        "Rename-Item -Path \"$t.new\" -NewName (Split-Path $t -Leaf); $swapped += $t }; "
        "if (Test-Path \"$secret.old\") { Remove-Item -Force \"$secret.old\" }; "
        "if (Test-Path $secret) { Rename-Item -Path $secret -NewName ((Split-Path $secret -Leaf) "
        "+ '.old'); $oldSecret = $true }; "
        "Move-Item -Path $placed -Destination $secret; $newSecret = $true; "
        f"{register}"
        "} catch { "
        "foreach ($t in $targets) { "
        "if ($swapped -contains $t) { Remove-Item -Recurse -Force $t -ErrorAction SilentlyContinue }; "
        "if ((-not (Test-Path $t)) -and (Test-Path \"$t.old\")) { "
        "Rename-Item -Path \"$t.old\" -NewName (Split-Path $t -Leaf) }; "
        "Remove-Item -Recurse -Force \"$t.new\" -ErrorAction SilentlyContinue }; "
        "if ($newSecret) { Remove-Item -Force $secret -ErrorAction SilentlyContinue }; "
        "Remove-Item -Force $placed -ErrorAction SilentlyContinue; "
        "if ($oldSecret) { Rename-Item -Path \"$secret.old\" -NewName (Split-Path $secret -Leaf) }; "
        "throw }; "
        "foreach ($old in ($targets | ForEach-Object { \"$_.old\" }) + \"$secret.old\") { "
        "Remove-Item -Recurse -Force $old -ErrorAction SilentlyContinue }; "
    )


def register_script(name: str, xml: str, sid: str) -> str:
    """Register the task in the \\pdt\\ folder, give the deploying user its rights, and delete
    the task an earlier pdt made in the root folder. A failure puts the earlier task back, or
    removes the new one, before the release script restores the files."""
    payload = base64.b64encode(xml.encode("utf-8")).decode("ascii")
    return (
        f"if (-not [Diagnostics.EventLog]::SourceExists({_ps_string(EVENT_SOURCE)})) {{ "
        f"New-EventLog -LogName Application -Source {_ps_string(EVENT_SOURCE)} }}; "
        f"$name = {_ps_string(name)}; "
        f"$xml = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{payload}')); "
        f"$current = Get-ScheduledTask -TaskName $name -TaskPath '{TASK_FOLDER}' "
        "-ErrorAction SilentlyContinue; "
        f"if ($null -ne $current -and -not \"$($current.Description)\".StartsWith('{MANAGED}')) {{ "
        f"throw \"Windows scheduled task {TASK_FOLDER}$name exists but is not managed by PDT\" }}; "
        "$previous = $null; "
        f"if ($null -ne $current) {{ $previous = Export-ScheduledTask -TaskName $name "
        f"-TaskPath '{TASK_FOLDER}' }}; "
        f"Register-ScheduledTask -TaskName $name -TaskPath '{TASK_FOLDER}' -Xml $xml -Force "
        "-ErrorAction Stop | Out-Null; "
        "try { $service = New-Object -ComObject Schedule.Service; $service.Connect(); "
        f"$service.GetFolder('\\{TASK_FOLDER_NAME}').GetTask($name)"
        f".SetSecurityDescriptor('{_task_sddl(sid)}', 0); "
        "$legacy = Get-ScheduledTask -TaskName $name -TaskPath '\\' -ErrorAction SilentlyContinue; "
        f"if ($null -ne $legacy -and \"$($legacy.Description)\".StartsWith('{MANAGED}')) {{ "
        "Unregister-ScheduledTask -InputObject $legacy -Confirm:$false } "
        "} catch { "
        f"if ($null -ne $previous) {{ Register-ScheduledTask -TaskName $name -TaskPath '{TASK_FOLDER}' "
        "-Xml $previous -Force | Out-Null } "
        f"else {{ Unregister-ScheduledTask -TaskName $name -TaskPath '{TASK_FOLDER}' -Confirm:$false }}; "
        "throw }; ")


def deploy(app: dict, assume_yes: bool, plan_only: bool = False) -> int:
    try:
        powershell = _preflight()
        name = _task_name(app["name"])
        path = _task_path(app["name"])
        console.status(f"Checking Windows scheduled task {console.value(path)}...")
        user, sid = _deploying_user()
        description, xml = task_xml(app)
        state = _task_state(path)
        if state == "unmanaged":
            raise WindowsDeployError(
                f"Windows scheduled task {path} exists but is not managed by PDT")
        exists = state != "absent"
        legacy_running = (
            f"{app['name']} is running from Windows task {_root_folder_path(app['name'])}, "
            f"which an earlier pdt made; wait for that run to end, then run pdt deploy "
            f"{app['name']} again, so the task in {TASK_FOLDER} does not start a second run "
            "beside it")
        legacy_state = _task_state(_root_folder_path(app["name"]))
        if legacy_state == "running":
            raise WindowsDeployError(legacy_running)
        legacy = legacy_state == "ready"
        pwsh, modules = _prepare_powershell(app)
        install_uv = not _machine_uv_current()
        uv = (windows_uv.fetch(os.environ.get("PROCESSOR_ARCHITECTURE", "AMD64"))
              if install_uv and not plan_only else None)
    except (config.ConfigError, WindowsDeployError, windows_uv.UvError) as exc:
        console.error(console.escape(str(exc)))
        return 1
    secrets = gather_secrets(app)

    actions = plan(app, "update" if exists else "create", description, user, secrets,
                   install_uv, pwsh, modules, legacy)
    cost = CostEstimate([("Task Scheduler on this Windows computer", 0.0)],
                        "no cloud charges", currency=regions.local_currency())
    if plan_only:
        return show_plan(actions, cost)
    if not confirm(actions, assume_yes, cost):
        console.warn("Aborted; nothing was changed.")
        return DECLINED

    release = stage_release(app, secrets, uv)
    install = ""
    if modules:
        command = _encoded(ps.install_command(modules))
        install = (f"& {_ps_string(pwsh)} -NoProfile -NonInteractive -EncodedCommand {command}; "
                   "if ($LASTEXITCODE -ne 0) { throw \"PowerShell module install failed "
                   "with exit $LASTEXITCODE\" }; ")
    # The root task can start a run after the check above; disabled, its schedule starts none
    # while the files change, and a run it already started stops the deploy.
    hold_legacy = (
        f"$root = Get-ScheduledTask -TaskName {_ps_string(name)} -TaskPath '\\' "
        "-ErrorAction SilentlyContinue; $rootDisabled = $false; "
        f"$rootManaged = $null -ne $root -and \"$($root.Description)\".StartsWith('{MANAGED}'); "
        "if ($rootManaged -and $root.Settings.Enabled) { "
        "Disable-ScheduledTask -InputObject $root | Out-Null; $rootDisabled = $true }; ")
    refuse_legacy_run = (
        "if ($rootManaged) { "
        "$service = New-Object -ComObject Schedule.Service; $service.Connect(); "
        f"if ($service.GetFolder('\\').GetTask({_ps_string(name)}).GetInstances(0).Count -gt 0) {{ "
        f"throw {_ps_string(legacy_running)} }} }}; ")
    script = ("$ErrorActionPreference = 'Stop'; " + SET_ACL + hold_legacy
              + f"try {{ {refuse_legacy_run}{_folder_script(app, user)}"
              + _release_script(app, release, install, register_script(name, xml, sid))
              + "} catch { if ($rootDisabled) { Enable-ScheduledTask -InputObject $root "
              "-ErrorAction SilentlyContinue | Out-Null }; throw }; ")
    try:
        with console.waiting(f"{'updating' if exists else 'creating'}: Windows scheduled task "
                             f"{console.value(path)}"):
            _run(powershell, script, elevate=True)
    except WindowsDeployError as exc:
        console.error(console.escape(str(exc)))
        return 1
    finally:
        shutil.rmtree(release, ignore_errors=True)
    console.done(f"Deployed {console.value(app['name'])} as Windows task {console.value(path)}.")
    return 0


def destroy(app: dict, assume_yes: bool) -> int:
    try:
        powershell = _preflight()
        name = _task_path(app["name"])
        console.status(f"Checking Windows scheduled task {console.value(name)}...")
        state = _task_state(name)
        if state == "unmanaged":
            raise WindowsDeployError(
                f"Windows scheduled task {name} exists but is not managed by PDT")
        exists = state != "absent"
        legacy = _task_state(_root_folder_path(app["name"])) in ("ready", "running")
    except WindowsDeployError as exc:
        console.error(console.escape(str(exc)))
        return 1
    parts = [(logs_folder(app["name"]), "run logs folder"),
             (deployed_folder(app["name"]), "deployed app folder"),
             (runtime_folder(app["name"]), "deployed pdt folder"),
             (secrets_file(app["name"]), "secrets file")]
    leftovers = [(Path(f"{path}{suffix}"), f"{label} ({suffix[1:]} copy)" if suffix else label)
                 for path, label in parts for suffix in ("", ".new", ".old", ".tmp")
                 if Path(f"{path}{suffix}").exists()]
    if not exists and not legacy and not leftovers:
        console.done(f"Nothing to remove for {console.value(app['name'])}; task "
                     f"{console.value(name)} does not exist.")
        if app["storage"]:
            console.say(_kept_storage_line(app["name"]))
        return 0
    actions = []
    script = ""
    for task, found in ((name, exists), (_root_folder_path(app["name"]), legacy)):
        if found:
            actions.append(console.Markup(f"delete Windows scheduled task {console.value(task)}"))
    for folder in (TASK_FOLDER, "\\"):
        script += (f"$task = Get-ScheduledTask -TaskName {_ps_string(_task_name(app['name']))} "
                   f"-TaskPath '{folder}' -ErrorAction SilentlyContinue; "
                   f"if ($null -ne $task -and \"$($task.Description)\".StartsWith('{MANAGED}')) {{ "
                   "Unregister-ScheduledTask -InputObject $task -Confirm:$false "
                   "-ErrorAction Stop }; ")
    script += ("$service = New-Object -ComObject Schedule.Service; $service.Connect(); "
               f"try {{ $folder = $service.GetFolder('\\{TASK_FOLDER_NAME}') }} catch {{ $folder = $null }}; "
               "if ($null -ne $folder -and $folder.GetTasks(1).Count -eq 0 "
               "-and $folder.GetFolders(0).Count -eq 0) { "
               f"$service.GetFolder('\\').DeleteFolder('{TASK_FOLDER_NAME}', 0) }}; ")
    for path, label in leftovers:
        actions.append(console.Markup(f"delete {console.escape(label)} {console.value(path)}"))
        script += f"Remove-Item -Recurse -Force -Path {_ps_string(str(path))} -ErrorAction Stop; "
    if app["storage"]:
        actions.append(console.Markup(f"keep folder {console.value(storage_folder(app['name']))} "
                                      "(the app's files)"))
        warn_if_locked(_store(app["name"]), app["name"])
    if not confirm(actions, assume_yes):
        console.warn("Aborted; nothing was changed.")
        return DECLINED
    removed = name if exists else _root_folder_path(app["name"])
    try:
        with console.waiting(f"removing: Windows scheduled task {console.value(removed)}"
                             if exists or legacy
                             else f"removing: the files of {console.value(app['name'])}"):
            _run(powershell, script, elevate=True)
    except WindowsDeployError as exc:
        console.error(console.escape(str(exc)))
        return 1
    console.done(f"Removed Windows task {console.value(removed)}." if exists or legacy
                 else f"Nothing to remove for {console.value(app['name'])}; task "
                 f"{console.value(name)} does not exist.")
    for path, label in leftovers:
        console.say(f"removed: {console.escape(label)} {console.value(path)}")
    if app["storage"]:
        console.say(_kept_storage_line(app["name"]))
    return 0


def _read_secret(powershell: str, app_name: str) -> str | None:
    secret = secrets_file(app_name)
    if not secret.exists():
        return None
    if _is_admin():
        return secret.read_text(encoding="utf-8-sig")
    folder = Path(tempfile.mkdtemp(prefix="pdt-secret-"))
    try:
        copy = folder / secret.name
        _run(powershell, f"Copy-Item -Path {_ps_string(str(secret))} -Destination "
                         f"{_ps_string(str(copy))} -ErrorAction Stop", elevate=True)
        return copy.read_text(encoding="utf-8-sig")
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def secrets(app: dict, action: str, assume_yes: bool, name: str | None = None) -> int:
    try:
        powershell = _preflight()
        current = _read_secret(powershell, app["name"])
    except WindowsDeployError as exc:
        console.error(console.escape(str(exc)))
        return 1

    def write(values: dict[str, str]) -> None:
        folder = Path(tempfile.mkdtemp(prefix="pdt-secret-"))
        try:
            staged = folder / SECRETS_FILE
            staged.write_text(json.dumps(values, sort_keys=True), encoding="utf-8")
            secret = secrets_file(app["name"])
            console.step(f"updating {console.value(secret)}")
            _run(powershell, f"$ErrorActionPreference = 'Stop'; {SET_ACL}"
                             f"$secret = {_ps_string(str(secret))}; $placed = \"$secret.tmp\"; "
                             "try { if (Test-Path $placed) { Remove-Item -Force $placed }; "
                             "New-Item -ItemType File -Path $placed | Out-Null; "
                             f"{set_acl('$placed', False)}"
                             "[IO.File]::WriteAllBytes($placed, [IO.File]::ReadAllBytes("
                             f"{_ps_string(str(staged))})); "
                             "if (Test-Path $secret) { "
                             "[IO.File]::Replace($placed, $secret, [NullString]::Value) } "
                             "else { Move-Item -Path $placed -Destination $secret }; "
                             f"{set_acl('$secret', False)}"
                             "} catch { Remove-Item -Force $placed -ErrorAction SilentlyContinue; throw }",
                 elevate=True)
        finally:
            shutil.rmtree(folder, ignore_errors=True)

    try:
        return run_secrets(action, app, current, write, assume_yes, name)
    except WindowsDeployError as exc:
        console.error(console.escape(str(exc)))
        return 1


def _managed_task(app: dict) -> tuple[str, str]:
    _preflight()
    path = _task_path(app["name"])
    state = _task_state(path)
    if state == "unmanaged":
        raise WindowsDeployError(
            f"Windows scheduled task {path} exists but is not managed by PDT")
    if state == "absent" and _task_state(_root_folder_path(app["name"])) in ("ready", "running"):
        raise WindowsDeployError(
            f"an earlier pdt made Windows task {_root_folder_path(app['name'])}; run pdt deploy "
            f"{app['name']} to move it to {path}")
    if state == "absent":
        raise WindowsDeployError(
            f"Windows scheduled task {path} does not exist, or another Windows account deployed "
            f"it; run pdt deploy {app['name']} first")
    return path, state


def _task_command(path: str, *args: str) -> None:
    proc = _schtasks(*args, "/TN", path)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        raise WindowsDeployError(
            f"Windows Task Scheduler refused {' '.join(args)} for {path}"
            + (f": {detail}" if detail else f" (exit {proc.returncode})")
            + "; deploy the app again from this Windows account")


def pause(app: dict, paused: bool) -> int:
    try:
        name, _state = _managed_task(app)
        _task_command(name, "/Change", "/DISABLE" if paused else "/ENABLE")
    except WindowsDeployError as exc:
        console.error(console.escape(str(exc)))
        return 1
    console.done(f"{'Paused' if paused else 'Unpaused'} {console.value(app['name'])}: Windows "
                 f"task {console.value(name)} "
                 f"is {'disabled' if paused else 'enabled'}.")
    return 0


def start(app: dict, follow: bool = False) -> int:
    before = {run.id for run in list_runs(app["name"])}
    marker = logs_folder(app["name"]) / MANUAL_START
    try:
        name, state = _managed_task(app)
        if state == "running":
            console.error(f"{console.value(app['name'])} is running now, and Windows task "
                          f"{console.value(name)} starts no second run while one runs; pdt "
                          "started none.")
            console.command(f"pdt logs {app['name']} --follow", "follow the run in progress")
            return 1
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch()
        _task_command(name, "/Run")
    except WindowsDeployError as exc:
        marker.unlink(missing_ok=True)
        console.error(console.escape(str(exc)))
        return 1
    console.done(f"Started {console.value(app['name'])}: Windows task {console.value(name)}.")
    if not follow:
        return 0
    deadline = time.monotonic() + runs_cli.NEW_RUN_TRIES * runs_cli.FOLLOW_SECONDS
    while (time.monotonic() < deadline and all(
            file.stem in before for file in logs_folder(app["name"]).glob("*.log"))):
        time.sleep(FOLLOW_POLL_SECONDS)
    return logs(app, ["--follow"], new=lambda run: run.id not in before)


def logs(app: dict, rest: list[str], new: Callable[[runs_cli.Run], bool] | None = None) -> int:
    return runs_cli.logs(lambda: list_runs(app["name"]),
                         lambda run, since: list(read_lines(app["name"], run)), app["name"], rest,
                         resolve=lambda found: resolve_runs(app["name"], found),
                         store="the run's log file",
                         stream=lambda run, follow: read_lines(app["name"], run, follow), new=new,
                         place=runs_cli.LogPlace("Logs folder", str(logs_folder(app["name"])),
                                                 "Log File", lambda runs: [f"{run.id}.log" for run in runs],
                                                 lambda: str(logs_folder(app["name"])),
                                                 lambda item: str(logs_folder(app["name"]) / item),
                                                 logs_folder(app["name"])))


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


def parse_line(raw: bytes) -> runs_cli.Line:
    text = raw.decode("utf-8-sig", "replace")
    match = LINE_TIME.match(text)
    if match is None:
        return runs_cli.parse_line(text, None)
    return runs_cli.parse_line(text[match.end():].removeprefix("\ufeff"),
                               datetime.fromisoformat(match.group(1)))


def read_lines(app_name: str, run: runs_cli.Run, follow: bool = False) -> Iterator[runs_cli.Line]:
    try:
        file = (logs_folder(app_name) / f"{run.id}.log").open("rb")
    except FileNotFoundError:
        return
    with file:
        rest = b""
        heard = time.monotonic()
        while True:
            chunk = file.read()
            *complete, rest = (rest + chunk).split(b"\n")
            for raw in complete:
                line = parse_line(raw)
                yield line
                if line.message.startswith(runs_cli.EXIT_MARKER):
                    return
            if not follow:
                return
            if chunk:
                heard = time.monotonic()
            elif time.monotonic() - heard >= runs_cli.STREAM_QUIET:
                return
            time.sleep(FOLLOW_POLL_SECONDS)


def _tail(file: Path) -> tuple[list[runs_cli.Line], float] | None:
    """The log's last whole lines, enough to hold its exit line, and its write time."""
    try:
        handle = file.open("rb")
    except FileNotFoundError:
        return None
    with handle:
        facts = os.fstat(handle.fileno())
        start = max(0, facts.st_size - EXIT_LINE_BYTES)
        handle.seek(start)
        tail = handle.read()
    if start > 0:
        tail = tail.partition(b"\n")[2]
    return [parse_line(raw) for raw in tail.splitlines()], facts.st_mtime


def list_runs(app_name: str) -> list[runs_cli.Run]:
    folder = logs_folder(app_name)
    if not folder.is_dir():
        return []
    stems = sorted((file.stem for file in folder.glob("*.log")), reverse=True)
    return [runs_cli.Run(stem, datetime.strptime(stem, "%Y%m%dT%H%M%SZ")
                         .replace(tzinfo=timezone.utc), None, "unknown") for stem in stems]


def resolve_runs(app_name: str, found: list[runs_cli.Run]) -> None:
    newest = max((file.stem for file in logs_folder(app_name).glob("*.log")), default=None)
    for run in found:
        file = logs_folder(app_name) / f"{run.id}.log"
        read = _tail(file)
        if read is None:
            continue
        lines, mtime = read
        running = False
        if run.id == newest and runs_cli.exit_code(lines) is None:
            running = _task_state(_task_path(app_name)) == "running"
            if not running:
                # The run can write its exit line and end while _task_state reads the task.
                lines, mtime = _tail(file) or read
        run.status = runs_cli.marker_status(lines, lambda: running)
        run.exit_code = runs_cli.exit_code(lines)
        run.ended = (datetime.fromtimestamp(mtime, tz=timezone.utc)
                     if run.exit_code is not None else None)


def _task_info(powershell: str, app_name: str) -> dict | None:
    script = (
        "$service = New-Object -ComObject Schedule.Service; $service.Connect(); "
        f"try {{ $task = $service.GetFolder('\\{TASK_FOLDER_NAME}')"
        f".GetTask({_ps_string(_task_name(app_name))}) }} catch {{ exit 3 }}; "
        "$times = @{}; foreach ($key in 'LastRunTime', 'NextRunTime') { $time = $task.$key; "
        "$times[$key] = if ($time.Year -ge 2000) { "
        "$time.ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ss.fffZ', "
        "[Globalization.CultureInfo]::InvariantCulture) } else { $null } }; "
        "ConvertTo-Json -Compress @{ enabled = $task.Enabled; result = $task.LastTaskResult; "
        "missed = $task.NumberOfMissedRuns; last_run = $times.LastRunTime; "
        "next_run = $times.NextRunTime }")
    proc = subprocess.run(
        [powershell, "-NoLogo", "-NoProfile", "-NonInteractive",
         "-ExecutionPolicy", "Bypass", "-EncodedCommand", _encoded(script)],
        stdin=subprocess.DEVNULL, capture_output=True, text=True)
    if proc.returncode == 3:
        return None
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        raise WindowsDeployError(
            "Windows Task Scheduler command failed"
            + (f": {detail}" if detail else f" (exit {proc.returncode})"))
    info = json.loads(proc.stdout)
    for key in ("last_run", "next_run"):
        info[key] = datetime.fromisoformat(info[key]) if info[key] else None
    return info


def task_problems(app: dict, info: dict,
                  logs: list[tuple[datetime, datetime]]) -> list[str]:
    """`logs` holds the start and last write of each run log. A log last written before the
    failed start belongs to a run that ended earlier, so it is not that start's log."""
    name = app["name"]
    problems = []
    if not info["enabled"] and not app.get("pause"):
        problems.append(f"Windows task {_task_path(name)} is disabled, so its schedule starts "
                        f"no run; run pdt unpause {name} to enable it")
    if info["missed"]:
        problems.append(
            f"Task Scheduler counts {info['missed']} missed runs of {name}: this PC was off or "
            "asleep at those times"
            + (f"; the next run is at {runs_cli.local_text(info['next_run'])}"
               if info["next_run"] else ""))
    result = info["result"] & 0xFFFFFFFF
    last = info["last_run"]
    if (last is not None and result not in (0, SCHED_S_TASK_RUNNING, SCHED_S_TASK_HAS_NOT_RUN)
            and not any(last - LOG_START_SLACK <= started <= last + LOG_START_WINDOW
                        and written >= last for started, written in logs)):
        problems.append(f"Task Scheduler could not start {name} at "
                        f"{runs_cli.local_text(last)} (result 0x{result:08X}), and that start "
                        f"wrote no log; run pdt deploy {name} to repair the deployed copy")
    return problems


def say_task_problems(app: dict) -> None:
    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    if powershell is None:
        return
    try:
        info = _task_info(powershell, app["name"])
    except WindowsDeployError as exc:
        console.warn(console.escape(str(exc)))
        return
    if info is None:
        return
    folder = logs_folder(app["name"])
    logs = [(run.started, datetime.fromtimestamp((folder / f"{run.id}.log").stat().st_mtime,
                                                 tz=timezone.utc))
            for run in list_runs(app["name"])]
    for problem in task_problems(app, info, logs):
        console.warn(console.escape(problem))


def remote(app: dict, host, command: str, rest: list[str], assume_yes: bool,
           plan: bool = False) -> int:
    problem = (config.windows_host_problem(host) if isinstance(host, str)
               else "write the name of the Windows PC, for example pc01.example.com")
    if problem != "":
        console.error(f"platform.host in {console.value(config.PROJECT_FILE)} or "
                      f"{console.value(app['name'] + '/' + config.APP_FILE)}: "
                      f"{console.escape(problem)}")
        return 1
    try:
        connection = windows_remote.connect(host, app["name"])
        if command == "login":
            return 0
        return windows_remote.forward(connection, app, command, rest, assume_yes, plan=plan)
    except (windows_remote.RemoteError, windows_uv.UvError) as exc:
        console.error(console.escape(str(exc)))
        return 1


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
    config.load_env(app["dir"])
    host = app["platform"].get("host")
    if host is not None and (not isinstance(host, str) or host.strip() != ""):
        return remote(app, host, args.command, args.rest, args.yes, args.plan)
    if sys.platform != "win32":
        console.error(
            "the windows provider targets this computer, but the current operating system "
            "is not Windows. Set host: under platform: in pdt.yml to the name of the Windows "
            "PC, or run this command on the Windows PC.")
        return 1
    if args.command == "login":
        console.note("the windows provider deploys to this computer, so it needs no login.")
        return 0
    if args.command == "secrets":
        return secrets(app, args.rest[0], args.yes, *args.rest[1:])
    if args.command == "storage":
        return storage(app, args.rest, args.yes)
    if args.command == "runs":
        say_task_problems(app)
        return runs_cli.runs(lambda: list_runs(app["name"]), app["name"], args.rest,
                             resolve=lambda found: resolve_runs(app["name"], found),
                             page="taskschd.msc",
                             note=f"Find the task {console.value(_task_path(app['name']))} "
                                  "in Task Scheduler Library.")
    if args.command == "logs":
        return logs(app, args.rest)
    if args.command in ("pause", "unpause"):
        return pause(app, args.command == "pause")
    if args.command == "start":
        return start(app, "--follow" in args.rest)
    if args.command == "deploy":
        return deploy(app, args.yes, args.plan)
    return destroy(app, args.yes)


if __name__ == "__main__":
    console.json_output()
    run_main(main)
