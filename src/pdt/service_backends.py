"""The table of service managers: one entry per platform, each with install, remove, and running.

Each entry starts `<python> -m pdt.cli service run`, except cron, which starts
`service tick` each minute and so holds no running process between ticks. On a root account,
systemd and launchd use the machine's folder, and on another account the user's own: pdt's data
folder belongs to the account, so the service runs as that account.
"""

from __future__ import annotations

import base64
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
CRON_MARK = "# pdt-service"
# The env vars that move pdt's data folder, so the service finds the same files as the installer.
KEPT_ENV = ("PDT_INSTALLATION_DIR", "XDG_DATA_HOME", "PDT_API_URL")


class BackendError(RuntimeError):
    pass


def service_command(verb: str = "run") -> list[str]:
    return [sys.executable, "-m", "pdt.cli", "service", verb]


def kept_env() -> dict[str, str]:
    return {name: os.environ[name] for name in KEPT_ENV if os.environ.get(name)}


def run(*args: str, stdin: str | None = None, check: bool = True) -> subprocess.CompletedProcess:
    options = {"input": stdin} if stdin is not None else {"stdin": subprocess.DEVNULL}
    proc = subprocess.run(list(args), capture_output=True, text=True, **options)
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        raise BackendError(f"{args[0]} failed: {detail or f'exit {proc.returncode}'}")
    return proc


def is_root() -> bool:
    return os.name != "nt" and os.geteuid() == 0


def task_xml(command: list[str], folder: Path) -> str:
    """The task starts at boot with no one signed in, as SYSTEM, and has no time limit. Task
    Scheduler starts it again each minute after a failure."""
    arguments = html.escape(subprocess.list2cmdline(command[1:]))
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>Managed by pdt; runs the pdt service.</Description></RegistrationInfo>
  <Triggers><BootTrigger><Enabled>true</Enabled></BootTrigger></Triggers>
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
    <RestartOnFailure><Interval>PT1M</Interval><Count>999</Count></RestartOnFailure>
    <Priority>7</Priority>
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


def windows_install(folder: Path) -> None:
    from pdt import deploy_windows
    payload = base64.b64encode(task_xml(service_command(), folder).encode("utf-8")).decode("ascii")
    name = deploy_windows._ps_string(NAME)
    powershell(
        "$ErrorActionPreference = 'Stop'; "
        f"$xml = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{payload}')); "
        f"Register-ScheduledTask -TaskName {name} -Xml $xml -Force -ErrorAction Stop | Out-Null; "
        f"Start-ScheduledTask -TaskName {name} -ErrorAction Stop")


def windows_remove() -> None:
    from pdt import deploy_windows
    name = deploy_windows._ps_string(NAME)
    powershell(f"$task = Get-ScheduledTask -TaskName {name} -ErrorAction SilentlyContinue; "
               f"if ($task) {{ Stop-ScheduledTask -TaskName {name}; "
               f"Unregister-ScheduledTask -TaskName {name} -Confirm:$false -ErrorAction Stop }}")


def windows_running() -> bool:
    from pdt import deploy_windows
    name = deploy_windows._ps_string(NAME)
    return powershell(f"(Get-ScheduledTask -TaskName {name} "
                      "-ErrorAction SilentlyContinue).State").strip() == "Running"


def systemd_path() -> Path:
    if is_root():
        return Path("/etc/systemd/system") / f"{NAME}.service"
    home = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return home / "systemd" / "user" / f"{NAME}.service"


def systemctl(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return run("systemctl", *([] if is_root() else ["--user"]), *args, check=check)


def systemd_install(_folder: Path) -> None:
    target = "multi-user.target" if is_root() else "default.target"
    environment = "".join(f"Environment={shlex.quote(f'{name}={value}')}\n"
                          for name, value in kept_env().items())
    path = systemd_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[Unit]\nDescription=pdt service\nAfter=network-online.target\n"
                    f"\n[Service]\nExecStart={shlex.join(service_command())}\n{environment}"
                    f"Restart=always\nRestartSec=30\n\n[Install]\nWantedBy={target}\n",
                    encoding="utf-8")
    systemctl("daemon-reload")
    systemctl("enable", "--now", f"{NAME}.service")
    if not is_root():
        # With no linger, a user's manager stops at sign-out and starts only at sign-in.
        run("loginctl", "enable-linger", check=False)


def systemd_remove() -> None:
    systemctl("disable", "--now", f"{NAME}.service", check=False)
    systemd_path().unlink(missing_ok=True)
    systemctl("daemon-reload", check=False)


def systemd_running() -> bool:
    return systemctl("is-active", "--quiet", f"{NAME}.service", check=False).returncode == 0


def launchd_path() -> Path:
    if is_root():
        return Path("/Library/LaunchDaemons") / f"{LAUNCHD_LABEL}.plist"
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def launchd_domain() -> str:
    return "system" if is_root() else f"gui/{os.getuid()}"


def launchd_install(folder: Path) -> None:
    path = launchd_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(plistlib.dumps({
        "Label": LAUNCHD_LABEL, "ProgramArguments": service_command(), "RunAtLoad": True,
        "KeepAlive": True, "EnvironmentVariables": kept_env(), "WorkingDirectory": str(folder),
        "StandardOutPath": str(folder / "launchd.log"),
        "StandardErrorPath": str(folder / "launchd.log")}))
    run("launchctl", "bootout", f"{launchd_domain()}/{LAUNCHD_LABEL}", check=False)
    run("launchctl", "bootstrap", launchd_domain(), str(path))


def launchd_remove() -> None:
    run("launchctl", "bootout", f"{launchd_domain()}/{LAUNCHD_LABEL}", check=False)
    launchd_path().unlink(missing_ok=True)


def launchd_running() -> bool:
    return run("launchctl", "print", f"{launchd_domain()}/{LAUNCHD_LABEL}",
               check=False).returncode == 0


def crontab_without_service() -> str:
    current = run("crontab", "-l", check=False)
    lines = current.stdout.splitlines() if current.returncode == 0 else []
    return "".join(f"{line}\n" for line in lines if CRON_MARK not in line)


def cron_install(_folder: Path) -> None:
    environment = " ".join(shlex.quote(f"{name}={value}") for name, value in kept_env().items())
    start = f"env {environment} " if environment else ""
    line = f"* * * * * {start}{shlex.join(service_command('tick'))} >/dev/null 2>&1 {CRON_MARK}\n"
    run("crontab", "-", stdin=crontab_without_service() + line)


def cron_remove() -> None:
    run("crontab", "-", stdin=crontab_without_service())


def cron_running() -> bool:
    return CRON_MARK in run("crontab", "-l", check=False).stdout


@dataclass(frozen=True)
class Backend:
    # Whether this manager exists on this computer.
    available: Callable[[], bool]
    install: Callable[[Path], None]
    remove: Callable[[], None]
    running: Callable[[], bool]


BACKENDS = {
    "windows": Backend(lambda: os.name == "nt", windows_install, windows_remove, windows_running),
    "systemd": Backend(lambda: bool(shutil.which("systemctl")) and Path("/run/systemd/system").is_dir(),
                       systemd_install, systemd_remove, systemd_running),
    "launchd": Backend(lambda: sys.platform == "darwin" and bool(shutil.which("launchctl")),
                       launchd_install, launchd_remove, launchd_running),
    "cron": Backend(lambda: bool(shutil.which("crontab")), cron_install, cron_remove, cron_running),
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
