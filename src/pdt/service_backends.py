"""The table of service managers, with the same five functions for each entry.

The pdt service runs only on Windows, so the table has one entry: a service of the Windows
Service Control Manager. It runs `<python> -m pdt.service_main` as SYSTEM, from the program folder
under the data folder, and it needs administrator rights. `define` writes the definition and
starts nothing, `start` starts it, and `remove` stops it and deletes the definition.

A second process of the service exits at once when one runs (`service.one_service`), so a start
while the service runs does nothing. The Service Control Manager starts it again after a failure
by its recovery actions.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

NAME = "pdt-service"
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


@dataclass(frozen=True)
class Backend:
    define: Callable[[str, Path], None]
    start: Callable[[str], None]
    remove: Callable[[], None]
    running: Callable[[], bool]


BACKENDS = {
    "windows": Backend(windows_define, windows_start, windows_remove, windows_running),
}


def choose() -> str:
    if os.name != "nt":
        raise BackendError("the pdt service runs only on Windows")
    return "windows"
