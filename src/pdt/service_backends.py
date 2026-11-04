"""The table of service managers: one entry per platform, with the same five functions.

Each entry runs `<python> -m pdt.cli service run` as SYSTEM or root, from the program folder under
the data folder, and each needs administrator rights. `define` writes the definition and starts
nothing, `start` starts it, and `remove` stops it and deletes the definition.

A second process of the service exits at once when one runs (`service.one_service`), so a trigger
that fires while the service runs does nothing. Each entry starts again after a failure: Task
Scheduler by a repeating trigger each 5 minutes, systemd by its restart rule, launchd by KeepAlive,
and cron each 5 minutes.
"""

from __future__ import annotations

import html
import os
import plistlib
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

NAME = "pdt-service"
LAUNCHD_LABEL = "com.autoidm.pdt-service"
SYSTEMD_PATH = Path("/etc/systemd/system") / f"{NAME}.service"
LAUNCHD_PATH = Path("/Library/LaunchDaemons") / f"{LAUNCHD_LABEL}.plist"
CRON_MARK = "# pdt-service"
# The env var that moves pdt's data folder, so the service finds the same files as the installer.
KEPT_ENV = ("PDT_INSTALLATION_DIR",)


class BackendError(RuntimeError):
    pass


def service_command(python: str, verb: str = "run") -> list[str]:
    return [python, "-m", "pdt.cli", "service", verb]


def kept_env() -> dict[str, str]:
    return {name: os.environ[name] for name in KEPT_ENV if os.environ.get(name)}


def run(*args: str, stdin: str | None = None, check: bool = True) -> subprocess.CompletedProcess:
    options = {"input": stdin} if stdin is not None else {"stdin": subprocess.DEVNULL}
    try:
        proc = subprocess.run(list(args), capture_output=True, text=True, **options)
    except OSError as e:
        raise BackendError(f"{args[0]} could not start: {e}") from None
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        raise BackendError(f"{args[0]} failed: {detail or f'exit {proc.returncode}'}")
    return proc


def task_xml(command: list[str], folder: Path) -> str:
    """The task starts at boot with no one signed in, as SYSTEM, has no time limit, and starts
    again by a trigger each 5 minutes. IgnoreNew does nothing while one instance runs."""
    arguments = html.escape(subprocess.list2cmdline(command[1:]))
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>Managed by pdt; runs the pdt service.</Description></RegistrationInfo>
  <Triggers>
    <BootTrigger><Enabled>true</Enabled></BootTrigger>
    <TimeTrigger>
      <Repetition><Interval>PT5M</Interval><StopAtDurationEnd>false</StopAtDurationEnd></Repetition>
      <StartBoundary>2000-01-01T00:00:00</StartBoundary>
      <Enabled>true</Enabled>
    </TimeTrigger>
  </Triggers>
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
    <Priority>5</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{html.escape(command[0])}</Command>
      <Arguments>{arguments}</Arguments>
      <WorkingDirectory>{html.escape(str(folder))}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>"""


def powershell(script: str) -> str:
    from pdt import deploy_windows
    found = shutil.which("powershell.exe") or shutil.which("powershell")
    if found is None:
        raise BackendError("Windows PowerShell was not found on PATH")
    try:
        return deploy_windows._run_output(found, script)
    except deploy_windows.WindowsDeployError as e:
        raise BackendError(str(e)) from None


def task_file(folder: Path) -> Path:
    return folder / "task.xml"


def windows_define(python: str, folder: Path) -> None:
    """Task Scheduler reads the definition from a file, because endpoint protection tools flag
    an encoded script."""
    path = task_file(folder)
    path.write_bytes(task_xml(service_command(python), folder).encode("utf-16"))
    run("schtasks", "/Create", "/TN", NAME, "/XML", str(path), "/F")


def windows_start(_python: str) -> None:
    run("schtasks", "/Run", "/TN", NAME)


def windows_remove() -> None:
    if run("schtasks", "/Query", "/TN", NAME, check=False).returncode != 0:
        return
    run("schtasks", "/End", "/TN", NAME, check=False)
    run("schtasks", "/Delete", "/TN", NAME, "/F")


def windows_running() -> bool:
    from pdt import deploy_windows
    name = deploy_windows._ps_string(NAME)
    return powershell(f"(Get-ScheduledTask -TaskName {name} "
                      "-ErrorAction SilentlyContinue).State").strip() == "Running"


def systemctl(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return run("systemctl", *args, check=check)


def systemd_define(python: str, _folder: Path) -> None:
    environment = "".join(f"Environment={shlex.quote(f'{name}={value}')}\n"
                          for name, value in kept_env().items())
    SYSTEMD_PATH.parent.mkdir(parents=True, exist_ok=True)
    SYSTEMD_PATH.write_text("[Unit]\nDescription=pdt service\nAfter=network-online.target\n"
                            f"\n[Service]\nExecStart={shlex.join(service_command(python))}\n"
                            f"{environment}Restart=always\nRestartSec=60\n\n"
                            "[Install]\nWantedBy=multi-user.target\n", encoding="utf-8")
    systemctl("daemon-reload")


def systemd_start(_python: str) -> None:
    systemctl("enable", "--now", f"{NAME}.service")


def systemd_remove() -> None:
    systemctl("disable", "--now", f"{NAME}.service", check=False)
    SYSTEMD_PATH.unlink(missing_ok=True)
    systemctl("daemon-reload", check=False)


def systemd_running() -> bool:
    return systemctl("is-active", "--quiet", f"{NAME}.service", check=False).returncode == 0


def launchd_define(python: str, folder: Path) -> None:
    LAUNCHD_PATH.parent.mkdir(parents=True, exist_ok=True)
    LAUNCHD_PATH.write_bytes(plistlib.dumps({
        "Label": LAUNCHD_LABEL, "ProgramArguments": service_command(python), "RunAtLoad": True,
        "KeepAlive": True, "EnvironmentVariables": kept_env(), "WorkingDirectory": str(folder),
        "AbandonProcessGroup": True, "StandardOutPath": str(folder / "launchd.log"),
        "StandardErrorPath": str(folder / "launchd.log")}))


def launchd_start(_python: str) -> None:
    run("launchctl", "bootout", f"system/{LAUNCHD_LABEL}", check=False)
    run("launchctl", "bootstrap", "system", str(LAUNCHD_PATH))


def launchd_remove() -> None:
    run("launchctl", "bootout", f"system/{LAUNCHD_LABEL}", check=False)
    LAUNCHD_PATH.unlink(missing_ok=True)


def launchd_running() -> bool:
    return run("launchctl", "print", f"system/{LAUNCHD_LABEL}", check=False).returncode == 0


def crontab_without_service() -> str:
    current = run("crontab", "-l", check=False)
    lines = current.stdout.splitlines() if current.returncode == 0 else []
    return "".join(f"{line}\n" for line in lines if CRON_MARK not in line)


def cron_lines(python: str) -> str:
    environment = " ".join(shlex.quote(f"{name}={value}") for name, value in kept_env().items())
    start = f"env {environment} " if environment else ""
    command = f"{start}{shlex.join(service_command(python))} >/dev/null 2>&1 {CRON_MARK}\n"
    return f"@reboot {command}*/5 * * * * {command}"


def cron_define(python: str, _folder: Path) -> None:
    run("crontab", "-", stdin=crontab_without_service() + cron_lines(python))


def cron_start(python: str) -> None:
    """The line each 5 minutes starts it too, but a person who installs it waits for the first
    check-in, so it starts at once, apart from this process."""
    subprocess.Popen(service_command(python), env={**os.environ, **kept_env()},
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)


def cron_remove() -> None:
    run("crontab", "-", stdin=crontab_without_service())


def cron_running() -> bool:
    return CRON_MARK in run("crontab", "-l", check=False).stdout


@dataclass(frozen=True)
class Backend:
    # Whether this manager exists on this computer.
    available: Callable[[], bool]
    define: Callable[[str, Path], None]
    start: Callable[[str], None]
    remove: Callable[[], None]
    running: Callable[[], bool]


BACKENDS = {
    "windows": Backend(lambda: os.name == "nt", windows_define, windows_start, windows_remove,
                       windows_running),
    "systemd": Backend(lambda: bool(shutil.which("systemctl")) and Path("/run/systemd/system").is_dir(),
                       systemd_define, systemd_start, systemd_remove, systemd_running),
    "launchd": Backend(lambda: sys.platform == "darwin" and bool(shutil.which("launchctl")),
                       launchd_define, launchd_start, launchd_remove, launchd_running),
    "cron": Backend(lambda: bool(shutil.which("crontab")), cron_define, cron_start, cron_remove,
                    cron_running),
}


def choose(requested: str | None) -> str:
    """The name of the backend: the one asked for, else the first this computer has."""
    if requested is not None:
        if not BACKENDS[requested].available():
            raise BackendError(f"{requested} does not exist on this computer")
        return requested
    for name, backend in BACKENDS.items():
        if backend.available():
            return name
    raise BackendError("this computer has no Task Scheduler, systemd, launchd, or cron")
