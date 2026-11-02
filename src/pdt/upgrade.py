"""`pdt upgrade`: move pdt, and the pdt-cli pin of each app in the project, to the newest release.

`uv tool install pdt-cli`, the MSI, and the winget zip all make pdt a uv tool, each in its own
tool folder, so the upgrade runs uv with the folders of the pdt that is running.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
import urllib.request
from pathlib import Path

from pdt import config, console
from pdt.config import ConfigError
from pdt.powershell import quoted

PYPI_URL = "https://pypi.org/pypi/pdt-cli/json"
PIN = re.compile(r'^(#[^\n]*"pdt-cli(?:\[[^\]]*\])?==)([^"]+)"', re.M)


def upgrade_in_new_window(uv: str, latest: str, environment: dict[str, str], entry: Path) -> None:
    # Windows cannot replace a running program, so the new window waits for this pdt and every
    # program that runs from its folders, such as the venv's python.exe and uv's pdt.exe.
    prefix = quoted(sys.prefix + "\\")
    script = (
        f"Wait-Process -Id {os.getpid()} -ErrorAction SilentlyContinue; "
        f"$running = @(Get-Process | Where-Object {{ $_.Path -eq {quoted(str(entry))} -or "
        f"($_.Path -and $_.Path.StartsWith({prefix}, 'OrdinalIgnoreCase')) }}); "
        "if ($running) { Write-Host 'pdt upgrade waits for these programs to end. Close them to "
        "continue:'; $running | ForEach-Object { Write-Host \"  $($_.Id) $($_.Path)\" }; "
        "$running | Wait-Process }; "
        f"$global:LASTEXITCODE = 1; & {quoted(uv)} tool install 'pdt-cli>={latest}'; "
        "if ($LASTEXITCODE) { Write-Host \"pdt upgrade failed: uv exited with code "
        "$LASTEXITCODE. pdt and the app pins stay as they are.\" } "
        f"else {{ & {quoted(str(entry))} upgrade }}; "
        "Read-Host 'Press Enter to close this window'")
    subprocess.Popen(["powershell", "-NoProfile", "-Command", script], env=environment,
                     creationflags=subprocess.CREATE_NEW_CONSOLE)


def raise_pins(latest: str) -> tuple[list[str], bool]:
    project = config.find_project()
    changed, failed = [], False
    for name in config.app_folders():
        run_py = project / name / "run.py"
        if not run_py.is_file():
            continue
        try:
            with open(run_py, encoding="utf-8", newline="") as file:
                text = file.read()
            pin = PIN.search(text)
            if pin is None or pin.group(2) == latest:
                continue
            with open(run_py, "w", encoding="utf-8", newline="") as file:
                file.write(PIN.sub(rf'\g<1>{latest}"', text))
        except (OSError, UnicodeError) as e:
            console.error(f"could not raise the pdt-cli pin in {console.value(str(run_py))}: "
                          f"{console.escape(str(e))}")
            failed = True
            continue
        console.bullet(f"{console.value(name)}: pdt-cli {console.value(pin.group(2))} "
                       f"to {console.value(latest)}")
        changed.append(name)
    return changed, failed


def main() -> int:
    from pdt import __version__
    try:
        with urllib.request.urlopen(PYPI_URL, timeout=30) as response:
            latest = json.load(response)["info"]["version"]
    except (OSError, ValueError, KeyError) as e:
        console.error(f"could not read the newest pdt release from {console.value(PYPI_URL)}: "
                      f"{console.escape(str(e))}")
        return 1
    receipt = Path(sys.prefix) / "uv-receipt.toml"
    if __version__ == latest:
        console.done(f"pdt {console.value(__version__)} is the newest release.")
    elif not receipt.is_file():
        console.note(f"pdt runs from {console.value(sys.prefix)}, which is not a uv tool, "
                     f"so pdt upgrade leaves it at {console.value(__version__)}.")
    else:
        tool = tomllib.loads(receipt.read_text(encoding="utf-8"))["tool"]
        entry = Path(next(e["install-path"] for e in tool["entrypoints"] if e["name"] == "pdt"))
        environment = {**os.environ, "UV_TOOL_DIR": str(receipt.parent.parent),
                       "UV_TOOL_BIN_DIR": str(entry.parent)}
        uv = shutil.which("uv")
        if uv is None:
            console.error("could not find uv on the PATH, so pdt and the app pins stay as they are.")
            return 1
        if sys.platform == "win32":
            upgrade_in_new_window(uv, latest, environment, entry)
            console.done(f"pdt {console.value(__version__)} moves to {console.value(latest)} in a "
                         "new window once this command ends. That window then raises the app pins.")
            return 0
        command = [uv, "tool", "install", f"pdt-cli>={latest}"]
        console.step(f"Upgrading pdt {console.value(__version__)} to {console.value(latest)}: "
                     f"{console.value(' '.join(command))}")
        code = subprocess.run(command, env=environment).returncode
        if code:
            console.error(f"uv exited with code {console.value(code)}, so pdt and the app pins stay as they are.")
            return code
    try:
        config.find_project()
    except ConfigError:
        return 0
    changed, failed = raise_pins(latest)
    deployed = [name for name in changed if config.is_deployed(name)]
    if deployed:
        console.next_steps([(f"pdt deploy {name}", "ship the new pdt to the deployed job")
                            for name in deployed])
    return 1 if failed else 0
