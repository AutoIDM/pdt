"""`pdt upgrade`: move pdt, and the pdt-cli pin of each app in the project, to the newest release.

pdt upgrades itself with `uv tool install "pdt-cli>=NEWEST"`, because both
`uv tool install pdt-cli` and the winget launcher install it as a uv tool. A
`uv tool upgrade` would keep a version the tool was installed at. Windows
cannot replace a program that is running, so there the upgrade runs in a new
window once this pdt has ended. A clone is left as it is.

Each app's run.py pins pdt-cli in its script header. That pin is the pdt the
app runs with, on this computer and once deployed, so it moves to the newest
release on PyPI, and the next deploy ships it.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

from pdt import config, console
from pdt.config import ConfigError

PYPI_URL = "https://pypi.org/pypi/pdt-cli/json"
# A pin on a comment line, which in run.py is the script header.
PIN = re.compile(r'^(#[^\n]*"pdt-cli(?:\[[^\]]*\])?==)([^"]+)"', re.M)


def upgrade_pdt(current: str, latest: str) -> int:
    if current == latest:
        console.done(f"pdt {console.value(current)} is the newest release.")
        return 0
    if not (Path(sys.prefix) / "uv-receipt.toml").is_file():
        console.note(f"pdt runs from {console.value(sys.prefix)}, which is not a uv tool, "
                     f"so pdt upgrade leaves it at {console.value(current)}.")
        return 0
    command = ["uv", "tool", "install", f"pdt-cli>={latest}"]
    if sys.platform == "win32":
        # This process and its parent, uv's pdt.exe, hold the files the upgrade replaces.
        script = (f"Wait-Process -Id {os.getpid()},{os.getppid()} -ErrorAction SilentlyContinue; "
                  f"uv tool install 'pdt-cli>={latest}'; Read-Host 'Press Enter to close this window'")
        subprocess.Popen(["powershell", "-NoProfile", "-Command", script],
                         creationflags=subprocess.CREATE_NEW_CONSOLE)
        console.done(f"pdt {console.value(current)} moves to {console.value(latest)} "
                     "in a new window once this command ends.")
        return 0
    console.step(f"Upgrading pdt {console.value(current)} to {console.value(latest)}: "
                 f"{console.value(' '.join(command))}")
    return subprocess.run(command).returncode


def raise_pins(latest: str) -> list[str]:
    """Raise the pdt-cli pin in each app's run.py to `latest`; return the apps it changed."""
    project = config.find_project()
    changed = []
    for name in config.app_folders():
        run_py = project / name / "run.py"
        if not run_py.is_file():
            continue
        # newline="" keeps a Windows file's line endings as they are.
        with open(run_py, encoding="utf-8", newline="") as file:
            text = file.read()
        pin = PIN.search(text)
        if pin is None or pin.group(2) == latest:
            continue
        with open(run_py, "w", encoding="utf-8", newline="") as file:
            file.write(PIN.sub(rf'\g<1>{latest}"', text))
        console.bullet(f"{console.value(name)}: pdt-cli {console.value(pin.group(2))} "
                       f"to {console.value(latest)}")
        changed.append(name)
    return changed


def main() -> int:
    from pdt import __version__
    try:
        with urllib.request.urlopen(PYPI_URL, timeout=30) as response:
            latest = json.load(response)["info"]["version"]
    except (OSError, ValueError, KeyError) as e:
        console.error(f"could not read the newest pdt release from {console.value(PYPI_URL)}: "
                      f"{console.escape(str(e))}")
        return 1
    code = upgrade_pdt(__version__, latest)
    try:
        config.find_project()
    except ConfigError:
        return code
    deployed = [name for name in raise_pins(latest) if config.is_deployed(name)]
    if deployed:
        console.next_steps([(f"pdt deploy {name}", "ship the new pdt to the deployed job")
                            for name in deployed])
    return code
