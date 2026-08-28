#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "pyyaml",
#     "python-dotenv",
# ]
# ///
"""Deploy an app locally as a Windows Task Scheduler task.

The task runs as the SYSTEM account, so it does not depend on a user being
logged on. Registering or removing it needs administrator rights; a
non-elevated shell gets one UAC prompt. Deploy always registers the complete
desired task definition with -Force, so rerunning it safely reconciles
changes to the schedule or repository path.
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import html
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pdt import config
from pdt.deploy import confirm


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


def _task_name(app_name: str) -> str:
    name = f"pdt-{app_name}"
    if any(char in FORBIDDEN_TASK_NAME_CHARS or ord(char) < 32 for char in name):
        raise WindowsDeployError(
            f"app name {app_name!r} contains characters Windows forbids in task names")
    return name


def task_xml(app: dict, uv: str) -> tuple[str, str]:
    cron = config.cron_expression(app["schedule"])
    timezone = str(app.get("timezone") or "").strip().lower()
    if timezone != "local":
        raise WindowsDeployError(
            "the Windows provider uses the machine's local timezone; "
            "set timezone: local for this app")
    description, trigger = schedule_trigger(cron)
    command = html.escape(str(Path(uv).resolve()))
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
      <Arguments>run --script run.py</Arguments>
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


def deploy(app: dict, assume_yes: bool) -> int:
    try:
        powershell, uv = _preflight()
        assert uv is not None
        name = _task_name(app["name"])
        description, xml = task_xml(app, uv)
        state = _task_state(powershell, name)
        if state == "unmanaged":
            raise WindowsDeployError(
                f"Windows scheduled task {name} exists but is not managed by PDT")
        exists = state == "managed"
    except (config.ConfigError, WindowsDeployError) as exc:
        print(f"error: {exc}")
        return 1

    verb = "update" if exists else "create"
    actions = [
        f"{verb} Windows scheduled task {name} (runs as SYSTEM)",
        f"run {app['name']} {description} (machine local time)",
        f"working directory: {app['dir']}",
    ]
    cost_lines = ["Estimated monthly platform cost: $0.00 "
                  "(uses this Windows computer)"]
    if not confirm(actions, assume_yes, cost_lines):
        print("Aborted; nothing was changed.")
        return 1

    payload = base64.b64encode(xml.encode("utf-8")).decode("ascii")
    script = (
        f"$name = {_ps_string(name)}; "
        f"$xml = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{payload}')); "
        "Register-ScheduledTask -TaskName $name -Xml $xml -Force "
        "-ErrorAction Stop | Out-Null"
    )
    try:
        _run(powershell, script, elevate=True)
    except WindowsDeployError as exc:
        print(f"error: {exc}")
        return 1
    print(f"Deployed {app['name']} as Windows task {name}.")
    print(f"Run it once now: Start-ScheduledTask -TaskName {_ps_string(name)}")
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
        print(f"error: {exc}")
        return 1
    if not exists:
        print(f"Nothing to remove for {app['name']}; task {name} does not exist.")
        return 0
    if not confirm([f"delete Windows scheduled task {name}"], assume_yes):
        print("Aborted; nothing was changed.")
        return 1
    try:
        _run(
            powershell,
            f"Unregister-ScheduledTask -TaskName {_ps_string(name)} "
            "-Confirm:$false -ErrorAction Stop",
            elevate=True,
        )
    except WindowsDeployError as exc:
        print(f"error: {exc}")
        return 1
    print(f"Removed Windows task {name}.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("deploy", "destroy", "login"))
    parser.add_argument("app")
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--profile", help="not used by Windows")
    args = parser.parse_args()
    if args.command == "login":
        print("The windows provider deploys to this computer, so it needs no login.")
        return 0
    try:
        app = config.merged_app(args.app)
    except config.ConfigError as exc:
        print(f"error: {exc}")
        return 1
    config.load_env(app["dir"])
    if args.command == "deploy":
        return deploy(app, args.yes)
    return destroy(app, args.yes)


if __name__ == "__main__":
    sys.exit(main())
