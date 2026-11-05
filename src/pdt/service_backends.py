"""The table of service managers: one entry per platform, with the same five functions.

Each entry runs `<python> -m pdt.service_main` as SYSTEM or root, from the program folder under
the data folder, and each needs administrator rights. `define` writes the definition and starts
nothing, `start` starts it, and `remove` stops it and deletes the definition.

A second process of the service exits at once when one runs (`service.one_service`), so a trigger
that fires while the service runs does nothing. Each entry starts again after a failure: the
Windows Service Control Manager by its recovery actions, systemd by its restart rule, launchd by
KeepAlive, and cron each 5 minutes.
"""

from __future__ import annotations

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
# The Service Control Manager takes the last action again for each later failure of the day.
RECOVERY = "restart/5000/restart/5000/restart/60000"
RESET_SECONDS = 86400
KEPT_ENV = ("PDT_INSTALLATION_DIR",)


class BackendError(RuntimeError):
    pass


def service_command(python: str) -> list[str]:
    return [python, "-m", "pdt.service_main"]


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


def windows_define(python: str, _folder: Path) -> None:
    """A service of the Service Control Manager: LocalSystem, started at boot, and started again
    5 seconds after the first and the second failure of a day, and 60 seconds after each later
    one. The failure flag also counts an exit with an error code. Each value that `kept_env` names
    goes in the service's Environment value."""
    binary = subprocess.list2cmdline(service_command(python))
    run("sc.exe", "create", NAME, "binPath=", binary, "start=", "auto", "obj=", "LocalSystem",
        "DisplayName=", "pdt service")
    run("sc.exe", "description", NAME, "Runs the commands of the AutoIDM portal. Managed by pdt.")
    run("sc.exe", "failure", NAME, "reset=", str(RESET_SECONDS), "actions=", RECOVERY)
    run("sc.exe", "failureflag", NAME, "1")
    environment = r"\0".join(f"{name}={value}" for name, value in kept_env().items())
    if environment:
        run("reg.exe", "add", rf"HKLM\SYSTEM\CurrentControlSet\Services\{NAME}", "/v",
            "Environment", "/t", "REG_MULTI_SZ", "/d", environment, "/f")


def windows_start(_python: str) -> None:
    run("net.exe", "start", NAME)


def windows_remove() -> None:
    if run("schtasks.exe", "/Query", "/TN", NAME, check=False).returncode == 0:
        run("schtasks.exe", "/End", "/TN", NAME, check=False)
        run("schtasks.exe", "/Delete", "/TN", NAME, "/F")
    if run("sc.exe", "query", NAME, check=False).returncode != 0:
        return
    run("net.exe", "stop", NAME, check=False)
    run("sc.exe", "delete", NAME)


def windows_running() -> bool:
    return " RUNNING" in run("sc.exe", "query", NAME, check=False).stdout


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
    subprocess.Popen(service_command(python), env={**os.environ, **kept_env()},
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)


def cron_remove() -> None:
    run("crontab", "-", stdin=crontab_without_service())


def cron_running() -> bool:
    return CRON_MARK in run("crontab", "-l", check=False).stdout


@dataclass(frozen=True)
class Backend:
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
    if requested is not None:
        if not BACKENDS[requested].available():
            raise BackendError(f"{requested} does not exist on this computer")
        return requested
    for name, backend in BACKENDS.items():
        if backend.available():
            return name
    raise BackendError("this computer has no Windows Service Control Manager, systemd, launchd, or cron")
