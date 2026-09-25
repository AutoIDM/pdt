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
# ]
# ///
"""Deploy an app locally as a Windows Task Scheduler task.

The task runs as the SYSTEM account, so it does not depend on a user being
logged on. Registering or removing it needs administrator rights; a
non-elevated shell gets one UAC prompt. Deploy always registers the complete
desired task definition with -Force, so rerunning it safely reconciles
changes to the schedule or repository path.

Each run writes its output to .pdt/runs/<app>/<UTC start>.log in the
project and ends it with `pdt: exit N`; `pdt runs` and `pdt logs` read
those files, and the task deletes files older than 30 days.
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import html
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pdt import config, console, runs_cli
from pdt.deploy import confirm
from pdt.deploy_common import CostEstimate


class WindowsDeployError(Exception):
    pass


MONTHS = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)
DAYS = ("Sunday", "Monday", "Tuesday", "Wednesday",
        "Thursday", "Friday", "Saturday")
FORBIDDEN_TASK_NAME_CHARS = set('\\/:*?"<>|')


def _single_number(field: str, label: str, lo: int, hi: int) -> int:
    try:
        value = int(field)
    except ValueError:
        raise WindowsDeployError(
            f"Windows Task Scheduler requires one numeric {label}; got {field!r}")
    if not lo <= value <= hi:
        raise WindowsDeployError(f"{label} must be between {lo} and {hi}")
    return value


def _number_set(field: str, label: str, lo: int, hi: int) -> list[int]:
    values: set[int] = set()
    try:
        for part in field.split(","):
            if "/" in part or part == "*":
                raise ValueError
            if "-" in part:
                first, last = (int(value) for value in part.split("-", 1))
            else:
                first = last = int(part)
            if first > last or first < lo or last > hi:
                raise ValueError
            values.update(range(first, last + 1))
    except ValueError:
        raise WindowsDeployError(
            f"Windows Task Scheduler cannot represent {label} field {field!r}; "
            "use numbers, comma-separated numbers, or ranges")
    return sorted(values)


def schedule_trigger(cron: str) -> tuple[str, str]:
    """Return (human description, Task Scheduler XML trigger fragment)."""
    minute_field, hour_field, dom, month, dow = cron.split()

    if hour_field == "*" and dom == month == dow == "*":
        if minute_field.startswith("*/"):
            interval = _single_number(minute_field[2:], "minute interval", 1, 59)
            if 60 % interval:
                raise WindowsDeployError(
                    f"cron minute interval {minute_field!r} resets each hour, "
                    "but a Windows Task Scheduler repetition interval does not; "
                    "use an interval that divides 60")
            minute = 0
            description = f"every {interval} minutes"
            repetition = f"<Interval>PT{interval}M</Interval>"
        else:
            minute = _single_number(minute_field, "minute", 0, 59)
            description = f"hourly at minute {minute:02d}"
            repetition = "<Interval>PT1H</Interval>"
        trigger = (
            f"<CalendarTrigger><Repetition>{repetition}<Duration>P1D</Duration>"
            "<StopAtDurationEnd>false</StopAtDurationEnd>"
            f"</Repetition><StartBoundary>2000-01-01T00:{minute:02d}:00"
            "</StartBoundary><Enabled>true</Enabled>"
            "<ScheduleByDay><DaysInterval>1</DaysInterval>"
            "</ScheduleByDay></CalendarTrigger>"
        )
        return description, trigger

    minute = _single_number(minute_field, "minute", 0, 59)
    hour = _single_number(hour_field, "hour", 0, 23)
    start = f"<StartBoundary>2000-01-01T{hour:02d}:{minute:02d}:00</StartBoundary>"

    if dom == month == dow == "*":
        return (
            f"daily at {hour:02d}:{minute:02d}",
            f"<CalendarTrigger>{start}<Enabled>true</Enabled><ScheduleByDay>"
            "<DaysInterval>1</DaysInterval></ScheduleByDay></CalendarTrigger>",
        )

    if dom == month == "*" and dow != "*":
        weekdays = {value % 7 for value in _number_set(dow, "day-of-week", 0, 7)}
        day_xml = "".join(f"<{DAYS[value]}/>" for value in sorted(weekdays))
        names = ", ".join(DAYS[value] for value in sorted(weekdays))
        return (
            f"weekly on {names} at {hour:02d}:{minute:02d}",
            f"<CalendarTrigger>{start}<Enabled>true</Enabled><ScheduleByWeek>"
            f"<WeeksInterval>1</WeeksInterval><DaysOfWeek>{day_xml}</DaysOfWeek>"
            "</ScheduleByWeek></CalendarTrigger>",
        )

    if dow == "*" and dom != "*":
        month_values = (list(range(1, 13)) if month == "*"
                        else _number_set(month, "month", 1, 12))
        day_values = _number_set(dom, "day-of-month", 1, 31)
        days_xml = "".join(f"<Day>{value}</Day>" for value in day_values)
        months_xml = "".join(f"<{MONTHS[value - 1]}/>" for value in month_values)
        month_words = "every month" if month == "*" else ", ".join(
            MONTHS[value - 1] for value in month_values)
        return (
            f"{month_words} on day {', '.join(map(str, day_values))} "
            f"at {hour:02d}:{minute:02d}",
            f"<CalendarTrigger>{start}<Enabled>true</Enabled><ScheduleByMonth>"
            f"<DaysOfMonth>{days_xml}</DaysOfMonth><Months>{months_xml}</Months>"
            "</ScheduleByMonth></CalendarTrigger>",
        )

    raise WindowsDeployError(
        f"cron {cron!r} cannot be represented faithfully by Windows Task "
        "Scheduler; use hourly, daily, weekly, monthly, yearly, '*/N * * * *', "
        "or a fixed-time cron restricted by either day-of-week or day-of-month")


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


def _run_script(app: dict, uv: str) -> str:
    """The script the scheduled task runs: log the app's run, then exit with its code."""
    folder = _ps_string(str(logs_folder(app["name"])))
    uv_path = _ps_string(str(Path(uv).resolve()))
    return (
        f"$folder = {folder}; "
        "New-Item -ItemType Directory -Force -Path $folder | Out-Null; "
        "$log = Join-Path $folder ([DateTime]::UtcNow.ToString('yyyyMMddTHHmmssZ') + '.log'); "
        f"& {uv_path} run --script run.py *>&1 | ForEach-Object {{ \"$_\" }} | "
        "Out-File -Encoding utf8 -FilePath $log; "
        "$code = $LASTEXITCODE; "
        "\"pdt: exit $code\" | Out-File -Encoding utf8 -Append -FilePath $log; "
        "Get-ChildItem -Path $folder -Filter *.log | "
        "Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-30) } | Remove-Item -Force; "
        "exit $code"
    )


def task_xml(app: dict, uv: str, powershell: str) -> tuple[str, str]:
    cron = config.cron_expression(app["schedule"])
    tz = str(app.get("timezone") or "").strip().lower()
    if tz != "local":
        raise WindowsDeployError(
            "the Windows provider uses the machine's local timezone; "
            "set timezone: local for this app")
    description, trigger = schedule_trigger(cron)
    command = html.escape(str(Path(powershell).resolve()))
    arguments = html.escape(
        "-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass "
        f"-EncodedCommand {_encoded(_run_script(app, uv))}")
    workdir = html.escape(str(Path(app["dir"]).resolve()))
    task_description = html.escape(
        f"Managed by pdt; runs {app['name']} from "
        f"{Path(app['dir']).resolve()}")
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
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
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


def _ps_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


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


def plan(app: dict, verb: str, description: str, user: str) -> list[str]:
    name = app["name"]
    actions = [
        f"{verb} Windows scheduled task {_task_name(name)} (runs as SYSTEM)",
        f"run {name} {description} (machine local time)",
        f"working directory: {app['dir']}",
        f"keep the app's run data in {app_folder(name)} (SYSTEM: full control; {user}: modify)",
        f"write one log per run under {logs_folder(name)} (removed on destroy)",
    ]
    if app["storage"]:
        actions.append(f"use folder {storage_folder(name)} for the app's files "
                       "(kept after destroy)")
    return actions


def deploy(app: dict, assume_yes: bool) -> int:
    try:
        powershell, uv = _preflight()
        assert uv is not None
        name = _task_name(app["name"])
        user = _deploying_user()
        description, xml = task_xml(app, uv, powershell)
        state = _task_state(powershell, name)
        if state == "unmanaged":
            raise WindowsDeployError(
                f"Windows scheduled task {name} exists but is not managed by PDT")
        exists = state == "managed"
    except (config.ConfigError, WindowsDeployError) as exc:
        console.error(str(exc))
        return 1

    actions = plan(app, "update" if exists else "create", description, user)
    cost = CostEstimate([("Task Scheduler on this Windows computer", 0.0)],
                        "no cloud charges")
    if not confirm(actions, assume_yes, cost):
        console.warn("Aborted; nothing was changed.")
        return 1

    payload = base64.b64encode(xml.encode("utf-8")).decode("ascii")
    script = (
        f"{_folder_script(app, user)}"
        f"$name = {_ps_string(name)}; "
        f"$xml = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{payload}')); "
        "Register-ScheduledTask -TaskName $name -Xml $xml -Force "
        "-ErrorAction Stop | Out-Null"
    )
    try:
        _run(powershell, script, elevate=True)
    except WindowsDeployError as exc:
        console.error(str(exc))
        return 1
    console.done(f"Deployed {app['name']} as Windows task {name}.")
    console.field("Run it once", f"Start-ScheduledTask -TaskName {_ps_string(name)}")
    console.field("Run history", f"Get-ScheduledTaskInfo -TaskName {_ps_string(name)}")
    console.bullet("or open Task Scheduler > Task Scheduler Library", indent=4)
    console.field("Run logs", str(logs_folder(app["name"])))
    return 0


def destroy(app: dict, assume_yes: bool) -> int:
    try:
        powershell, _uv = _preflight(require_uv=False)
        name = _task_name(app["name"])
        state = _task_state(powershell, name)
        if state == "unmanaged":
            raise WindowsDeployError(
                f"Windows scheduled task {name} exists but is not managed by PDT")
        exists = state == "managed"
    except WindowsDeployError as exc:
        console.error(str(exc))
        return 1
    if not exists:
        console.done(f"Nothing to remove for {app['name']}; task {name} does not exist.")
        if app["storage"]:
            console.say(_kept_storage_line(app["name"]))
        return 0
    if not confirm([f"delete Windows scheduled task {name}"], assume_yes):
        console.warn("Aborted; nothing was changed.")
        return 1
    try:
        _run(
            powershell,
            f"Unregister-ScheduledTask -TaskName {_ps_string(name)} "
            "-Confirm:$false -ErrorAction Stop",
            elevate=True,
        )
    except WindowsDeployError as exc:
        console.error(str(exc))
        return 1
    console.done(f"Removed Windows task {name}.")
    if app["storage"]:
        console.say(_kept_storage_line(app["name"]))
    return 0


def _kept_storage_line(app_name: str) -> str:
    folder = storage_folder(app_name)
    count = sum(1 for file in folder.rglob("*") if file.is_file()) if folder.is_dir() else 0
    return f"kept: folder {folder} ({count} files)"


def storage(app: dict, rest: list[str], assume_yes: bool) -> int:
    from pdt import storage_cli
    from pdt.utils.storage import Store

    folder = storage_folder(app["name"])
    store = Store(folder.as_uri() + "/", None)
    return storage_cli.run(store, app, rest, assume_yes)


def read_lines(app_name: str, run: runs_cli.Run) -> list[runs_cli.Line]:
    try:
        text = (logs_folder(app_name) / f"{run.id}.log").read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return []
    return [runs_cli.parse_line(line, None) for line in text.splitlines()]


def list_runs(app_name: str) -> list[runs_cli.Run]:
    folder = logs_folder(app_name)
    if not folder.is_dir():
        return []
    files = sorted(folder.glob("*.log"), key=lambda file: file.name, reverse=True)
    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    task_name = _task_name(app_name)
    found = []
    for file in files:
        started = datetime.strptime(file.stem, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        lines = [runs_cli.parse_line(line, None) for line in
                file.read_text(encoding="utf-8-sig").splitlines()]
        status = runs_cli.marker_status(
            lines, lambda: powershell is not None and _task_running(powershell, task_name))
        code = runs_cli.exit_code(lines)
        ended = (datetime.fromtimestamp(file.stat().st_mtime, tz=timezone.utc)
                if code is not None else None)
        found.append(runs_cli.Run(file.stem, started, ended, status, code))
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command",
                        choices=("deploy", "destroy", "login", "storage", "secrets",
                                 "runs", "logs"))
    parser.add_argument("app")
    parser.add_argument("rest", nargs="*")
    parser.add_argument("--yes", action="store_true")
    args = parser.parse_intermixed_args()
    if args.command == "login":
        console.note("the windows provider deploys to this computer, so it needs no login.")
        return 0
    if args.command == "secrets":
        console.note("the windows provider reads your .env file at every run, "
                     "so there is nothing to compare, save, get, or set.")
        return 0
    try:
        app = config.merged_app(args.app)
    except config.ConfigError as exc:
        console.error(str(exc))
        return 1
    config.load_env(app["dir"])
    if args.command == "storage":
        return storage(app, args.rest, args.yes)
    if args.command == "runs":
        return runs_cli.runs(lambda: list_runs(app["name"]), app["name"], args.rest)
    if args.command == "logs":
        return runs_cli.logs(lambda: list_runs(app["name"]),
                             lambda run: read_lines(app["name"], run), app["name"], args.rest)
    if args.command == "deploy":
        return deploy(app, args.yes)
    return destroy(app, args.yes)


if __name__ == "__main__":
    sys.exit(main())
