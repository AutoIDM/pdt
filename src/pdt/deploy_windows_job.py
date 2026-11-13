"""Deploy a job locally as a Windows Task Scheduler task.

Per app on this PC there is one folder, `%ProgramData%\\pdt\\<app>\\`, with
`storage\\` (the app's files, kept after destroy) and `logs\\` (one log per
run, removed on destroy), and one scheduled task `pdt-<app>` whose action
runs `run_windows_task.py` through uv. The runner sets PDT_STORAGE_URL to the
storage folder, runs the app in its folder, and writes both output streams
to `logs\\<UTC start>.log`, each line after its time, ending with
`pdt: exit N`; `pdt runs` and `pdt logs` read those files. A run is
stopped after RUN_TIME_LIMIT.

The task's action is `<uv> run --script run_windows_task.py <app dir>
<logs folder> <storage url>`. SYSTEM sees only the machine PATH, so `<uv>`
is the bare word `uv` when uv is on that PATH at deploy time and otherwise
the absolute path deploy found. Either way `uv run` hands the runner the uv
that started it in `UV`, so the runner never looks uv up again.

The task runs as the SYSTEM account, so it does not depend on a user being
logged on. Registering or removing it needs administrator rights; a
non-elevated shell gets one UAC prompt. The same elevated script creates the
app folder and gives SYSTEM full control and the deploying user modify
rights, so that user can delete what the task wrote. Deploy always registers
the complete desired task definition with -Force, so rerunning it safely
reconciles changes to the schedule or repository path.

A PowerShell app (.ps1 files, no run.py) runs through run_powershell.py
instead of run.py, unless `pdt new APP --from-scripts` wrote it one. Before
the plan, deploy finds pwsh (`pdt.pwsh` installs the pinned version under
%ProgramData%\\pdt\\pwsh when none is on the PATH), scans the scripts, and
stops with the Windows feature to add when a module that comes with Windows
(`powershell.WINDOWS_IN_BOX_MODULES`) is missing. The
elevated script installs the other modules for all users, so SYSTEM sees them.

The shared pieces (folders, the PowerShell runner, the task state check) are
in `deploy_windows.py`, reached as `win.` so a test that patches them there
patches them here too.
"""

from __future__ import annotations

import base64
import html
import os
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator

from pdt import config, console, deploy_windows as win, regions, runs_cli
from pdt import powershell as ps
from pdt.deploy_common import CostEstimate, warn_if_locked
from pdt.run_windows_task import EVENT_SOURCE, LINE_TIME, MANUAL_START

MONTHS = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)
DAYS = ("Sunday", "Monday", "Tuesday", "Wednesday",
        "Thursday", "Friday", "Saturday")
RUNNER = Path(__file__).resolve().with_name("run_windows_task.py")
# The same limit as --replica-timeout on Azure Container Apps.
RUN_TIME_LIMIT = "PT30M"
FOLLOW_POLL_SECONDS = 0.25
EXIT_LINE_BYTES = 256


def _number_set(field: str) -> list[int]:
    values: set[int] = set()
    for part in field.split(","):
        first, _, last = part.partition("-")
        values.update(range(int(first), int(last or first) + 1))
    return sorted(values)


def schedule_trigger(cron: str) -> tuple[str, str]:
    """Return (human description, Task Scheduler XML trigger fragment)."""
    problem = config.windows_schedule_problem(cron)
    if problem != "":
        raise win.WindowsDeployError(problem)
    minute_field, hour_field, dom, month, dow = cron.split()

    if hour_field == "*" and dom == month == dow == "*":
        if minute_field.startswith("*/"):
            interval = int(minute_field[2:])
            minute = 0
            description = f"every {interval} minutes"
            repetition = f"<Interval>PT{interval}M</Interval>"
        else:
            minute = int(minute_field)
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

    minute = int(minute_field)
    hour = int(hour_field)
    start = f"<StartBoundary>2000-01-01T{hour:02d}:{minute:02d}:00</StartBoundary>"

    if dom == month == dow == "*":
        return (
            f"daily at {hour:02d}:{minute:02d}",
            f"<CalendarTrigger>{start}<Enabled>true</Enabled><ScheduleByDay>"
            "<DaysInterval>1</DaysInterval></ScheduleByDay></CalendarTrigger>",
        )

    if dom == month == "*" and dow != "*":
        weekdays = {value % 7 for value in _number_set(dow)}
        day_xml = "".join(f"<{DAYS[value]}/>" for value in sorted(weekdays))
        names = ", ".join(DAYS[value] for value in sorted(weekdays))
        return (
            f"weekly on {names} at {hour:02d}:{minute:02d}",
            f"<CalendarTrigger>{start}<Enabled>true</Enabled><ScheduleByWeek>"
            f"<WeeksInterval>1</WeeksInterval><DaysOfWeek>{day_xml}</DaysOfWeek>"
            "</ScheduleByWeek></CalendarTrigger>",
        )

    month_values = list(range(1, 13)) if month == "*" else _number_set(month)
    day_values = _number_set(dom)
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


def task_arguments(app: dict) -> str:
    """The uv arguments the task runs: the runner, then the app folder, its log folder,
    and its storage url. Quoted the way CreateProcess splits them."""
    return subprocess.list2cmdline([
        "run", "--script", str(RUNNER), str(Path(app["dir"]).resolve()),
        str(win.logs_folder(app["name"])), win.storage_url(app["name"])])


def task_xml(app: dict, uv: str, on_machine_path: bool) -> tuple[str, str]:
    cron = config.cron_expression(app["schedule"])
    description, trigger = schedule_trigger(cron)
    command = "uv" if on_machine_path else html.escape(str(Path(uv).resolve()))
    arguments = html.escape(task_arguments(app))
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
    <Enabled>{"false" if app.get("pause") else "true"}</Enabled>
    <Hidden>false</Hidden>
    <ExecutionTimeLimit>{RUN_TIME_LIMIT}</ExecutionTimeLimit>
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


def plan(app: dict, verb: str, description: str, user: str, uv: str,
         on_machine_path: bool, pwsh: str | None = None,
         modules: list = ()) -> list[console.Markup]:
    name = app["name"]
    actions = [
        console.Markup(f"{verb} Windows scheduled task {console.value(win._task_name(name))} "
                       "(runs as SYSTEM)"),
        console.Markup(f"run {console.value(name)} {console.escape(description)} "
                       "(machine local time)" + (" (paused)" if app.get("pause") else "")),
        console.Markup(f"working directory: {console.value(app['dir'])}"),
        console.Markup("run uv from the system PATH" if on_machine_path else
                       f"run uv from {console.value(Path(uv).resolve())}"),
        console.Markup(f"keep the app's run data in {console.value(win.app_folder(name))} "
                       f"(SYSTEM: full control; {console.value(user)}: modify)"),
        console.Markup(f"write one log per run under {console.value(win.logs_folder(name))} "
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
        actions.append(console.Markup(f"use folder {console.value(win.storage_folder(name))} for "
                                      "the app's files (kept after destroy)"))
    if pwsh is not None:
        actions.append(console.Markup(f"run the app's .ps1 files with PowerShell from "
                                      f"{console.value(pwsh)}"))
    if modules:
        names = ", ".join(console.value(need.name) for need in modules)
        actions.append(console.Markup(f"install PowerShell modules {names} (all users)"))
    return actions


def deploy(app: dict, assume_yes: bool, plan_only: bool = False) -> int:
    try:
        powershell, uv = win._preflight()
        assert uv is not None
        on_machine_path = win.uv_on_machine_path()
        name = win._task_name(app["name"])
        console.status(f"Checking Windows scheduled task {console.value(name)}...")
        user = win._deploying_user()
        description, xml = task_xml(app, uv, on_machine_path)
        state = win._task_state(powershell, name)
        if state == "unmanaged":
            raise win.WindowsDeployError(
                f"Windows scheduled task {name} exists but is not managed by PDT")
        exists = state == "managed"
        pwsh, modules = win._prepare_powershell(app)
    except (config.ConfigError, win.WindowsDeployError) as exc:
        console.error(console.escape(str(exc)))
        return 1

    actions = plan(app, "update" if exists else "create", description, user, uv,
                   on_machine_path, pwsh, modules)
    cost = CostEstimate([("Task Scheduler on this Windows computer", 0.0)],
                        "no cloud charges", currency=regions.local_currency())
    if plan_only:
        return win.show_plan(actions, cost)
    if not win.confirm(actions, assume_yes, cost):
        console.warn("Aborted; nothing was changed.")
        return win.DECLINED

    payload = base64.b64encode(xml.encode("utf-8")).decode("ascii")
    install = ""
    if modules:
        command = win._encoded(ps.install_command(modules))
        install = (f"& {win._ps_string(pwsh)} -NoProfile -NonInteractive -EncodedCommand "
                   f"{command}; if ($LASTEXITCODE -ne 0) {{ throw \"PowerShell module install "
                   "failed with exit $LASTEXITCODE\" }; ")
    script = (
        f"{win._folder_script(app, user)}"
        f"{install}"
        f"if (-not [Diagnostics.EventLog]::SourceExists({win._ps_string(EVENT_SOURCE)})) {{ "
        f"New-EventLog -LogName Application -Source {win._ps_string(EVENT_SOURCE)} }}; "
        f"$name = {win._ps_string(name)}; "
        f"$xml = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{payload}')); "
        "Register-ScheduledTask -TaskName $name -Xml $xml -Force "
        "-ErrorAction Stop | Out-Null"
    )
    try:
        with console.waiting(f"{'updating' if exists else 'creating'}: Windows scheduled task "
                             f"{console.value(name)}"):
            win._run(powershell, script, elevate=True)
    except win.WindowsDeployError as exc:
        console.error(console.escape(str(exc)))
        return 1
    console.done(f"Deployed {console.value(app['name'])} as Windows task {console.value(name)}.")
    return 0


def destroy(app: dict, assume_yes: bool) -> int:
    try:
        powershell, _uv = win._preflight(require_uv=False)
        name = win._task_name(app["name"])
        console.status(f"Checking Windows scheduled task {console.value(name)}...")
        state = win._task_state(powershell, name)
        if state == "unmanaged":
            raise win.WindowsDeployError(
                f"Windows scheduled task {name} exists but is not managed by PDT")
        exists = state == "managed"
    except win.WindowsDeployError as exc:
        console.error(console.escape(str(exc)))
        return 1
    logs = win.logs_folder(app["name"])
    logs_exist = logs.is_dir()
    if not exists and not logs_exist:
        console.done(f"Nothing to remove for {console.value(app['name'])}; task "
                     f"{console.value(name)} does not exist.")
        if app["storage"]:
            console.say(win._kept_storage_line(app["name"]))
        return 0
    actions = []
    script = ""
    if exists:
        actions.append(console.Markup(f"delete Windows scheduled task {console.value(name)}"))
        script += (f"Unregister-ScheduledTask -TaskName {win._ps_string(name)} "
                   "-Confirm:$false -ErrorAction Stop; ")
    if logs_exist:
        actions.append(console.Markup(f"delete run logs folder {console.value(logs)}"))
        script += (f"Remove-Item -Recurse -Force -Path {win._ps_string(str(logs))} "
                   "-ErrorAction Stop; ")
    if app["storage"]:
        actions.append(console.Markup(
            f"keep folder {console.value(win.storage_folder(app['name']))} (the app's files)"))
        warn_if_locked(win._store(app["name"]), app["name"])
    if not win.confirm(actions, assume_yes):
        console.warn("Aborted; nothing was changed.")
        return win.DECLINED
    try:
        with console.waiting(f"removing: Windows scheduled task {console.value(name)}" if exists
                             else f"removing: {console.value(logs)}"):
            win._run(powershell, script, elevate=True)
    except win.WindowsDeployError as exc:
        console.error(console.escape(str(exc)))
        return 1
    console.done(f"Removed Windows task {console.value(name)}." if exists
                 else f"Nothing to remove for {console.value(app['name'])}; task "
                 f"{console.value(name)} does not exist.")
    if logs_exist:
        console.say(f"removed: run logs folder {console.value(logs)}")
    if app["storage"]:
        console.say(win._kept_storage_line(app["name"]))
    return 0


def _managed_task(app: dict) -> tuple[str, str]:
    """The PowerShell path and the task name, once the task exists and is pdt's."""
    powershell, _uv = win._preflight(require_uv=False)
    name = win._task_name(app["name"])
    state = win._task_state(powershell, name)
    if state == "unmanaged":
        raise win.WindowsDeployError(
            f"Windows scheduled task {name} exists but is not managed by PDT")
    if state == "absent":
        raise win.WindowsDeployError(
            f"Windows scheduled task {name} does not exist; run pdt deploy {app['name']} first")
    return powershell, name


def pause(app: dict, paused: bool) -> int:
    verb = "Disable-ScheduledTask" if paused else "Enable-ScheduledTask"
    try:
        powershell, name = _managed_task(app)
        win._run(powershell, f"{verb} -TaskName {win._ps_string(name)} -ErrorAction Stop "
                             "| Out-Null", elevate=True)
    except win.WindowsDeployError as exc:
        console.error(console.escape(str(exc)))
        return 1
    console.done(f"{'Paused' if paused else 'Unpaused'} {console.value(app['name'])}: Windows "
                 f"task {console.value(name)} "
                 f"is {'disabled' if paused else 'enabled'}.")
    return 0


def start(app: dict, follow: bool = False) -> int:
    before = {run.id for run in list_runs(app["name"])}
    try:
        powershell, name = _managed_task(app)
        win.logs_folder(app["name"]).mkdir(parents=True, exist_ok=True)
        (win.logs_folder(app["name"]) / MANUAL_START).touch()
        win._run(powershell, f"Start-ScheduledTask -TaskName {win._ps_string(name)} "
                             "-ErrorAction Stop", elevate=True)
    except win.WindowsDeployError as exc:
        console.error(console.escape(str(exc)))
        return 1
    console.done(f"Started {console.value(app['name'])}: Windows task {console.value(name)}.")
    if not follow:
        return 0
    deadline = time.monotonic() + runs_cli.NEW_RUN_TRIES * runs_cli.FOLLOW_SECONDS
    while (time.monotonic() < deadline and all(
            file.stem in before for file in win.logs_folder(app["name"]).glob("*.log"))):
        time.sleep(FOLLOW_POLL_SECONDS)
    return logs(app, ["--follow"], new=lambda run: run.id not in before)


def logs(app: dict, rest: list[str], new: Callable[[runs_cli.Run], bool] | None = None) -> int:
    return log_command(app, rest, lambda: list_runs(app["name"]), new)


def log_command(app: dict, rest: list[str], runs: Callable[[], list[runs_cli.Run]],
                new: Callable[[runs_cli.Run], bool] | None = None) -> int:
    """`pdt logs` over the log files in the app's logs folder; the web profile shares it."""
    folder = win.logs_folder(app["name"])
    return runs_cli.logs(runs,
                         lambda run, since: list(read_lines(app["name"], run)), app["name"], rest,
                         store="the run's log file",
                         stream=lambda run, follow: read_lines(app["name"], run, follow), new=new,
                         place=runs_cli.LogPlace("Logs folder", str(folder),
                                                 "Log File", lambda runs: [f"{run.id}.log" for run in runs],
                                                 lambda: str(folder),
                                                 lambda item: str(folder / item),
                                                 folder))


def parse_line(raw: bytes) -> runs_cli.Line:
    text = raw.decode("utf-8-sig", "replace")
    match = LINE_TIME.match(text)
    if match is None:
        return runs_cli.parse_line(text, None)
    return runs_cli.parse_line(text[match.end():].removeprefix("﻿"),
                               datetime.fromisoformat(match.group(1)))


def read_lines(app_name: str, run: runs_cli.Run, follow: bool = False) -> Iterator[runs_cli.Line]:
    try:
        file = (win.logs_folder(app_name) / f"{run.id}.log").open("rb")
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
    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    task_name = win._task_name(app_name)
    return runs_from_logs(app_name, lambda: powershell is not None
                          and win._task_running(powershell, task_name))


def runs_from_logs(app_name: str, running: Callable[[], bool]) -> list[runs_cli.Run]:
    """One run per log file in the app's logs folder, newest first. `running` says whether
    the process behind the newest log, when it has no exit line yet, is still going."""
    folder = win.logs_folder(app_name)
    if not folder.is_dir():
        return []
    files = sorted(folder.glob("*.log"), key=lambda file: file.name, reverse=True)
    found = []
    newest = files[0] if files else None
    for file in files:
        started = datetime.strptime(file.stem, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        read = _tail(file)
        if read is None:
            continue
        lines, mtime = read
        going = False
        if file == newest and runs_cli.exit_code(lines) is None:
            going = running()
            if not going:
                # The run can write its exit line and end while `running` waits for powershell.
                lines, mtime = _tail(file) or read
        status = runs_cli.marker_status(lines, lambda: going)
        code = runs_cli.exit_code(lines)
        ended = datetime.fromtimestamp(mtime, tz=timezone.utc) if code is not None else None
        found.append(runs_cli.Run(file.stem, started, ended, status, code))
    return found


def command(app: dict, name: str, rest: list[str], assume_yes: bool, plan_only: bool) -> int:
    """One provider command for a job; `deploy_windows.main` calls it."""
    if name == "runs":
        return runs_cli.runs(lambda: list_runs(app["name"]), app["name"], rest,
                             page="taskschd.msc",
                             note=f"Find the task {console.value(win._task_name(app['name']))} "
                                  "in Task Scheduler Library.")
    if name == "logs":
        return logs(app, rest)
    if name in ("pause", "unpause"):
        return pause(app, name == "pause")
    if name == "start":
        return start(app, "--follow" in rest)
    if name == "restart":
        console.error(f"{console.value(app['name'])} is a job; it has no server to restart. "
                      f"{console.value(f'pdt run {app['name']} --deployed')} starts a run.")
        return 1
    if name == "deploy":
        return deploy(app, assume_yes, plan_only)
    return destroy(app, assume_yes)
