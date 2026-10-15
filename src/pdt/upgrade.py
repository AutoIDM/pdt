"""`pdt upgrade`: move pdt, and the pdt-cli pin of each app in the project, to the newest release.

The newest release is the version PyPI names for pdt-cli. pdt replaces itself
the way it was installed:

- A uv tool (`uv tool install pdt-cli`) gets `uv tool install "pdt-cli>=NEWEST"`,
  which leaves `uv tool upgrade pdt-cli` working afterwards.
- The winget package (`winget install AutoIDM.pdt`) gets `winget upgrade`. Its
  launcher installs its own pdt-cli version on every start, so a uv upgrade
  under it would be undone the next time pdt starts.
- A clone (`./pdt`, `.\\pdt.bat`, or an editable install) gets
  `git pull --ff-only` when it is on main with no changes, and is left alone
  otherwise.

Windows does not let a program replace a file that is running, so there the uv
or winget upgrade runs in a new window once each pdt process of this command
has ended.

Inside a project, the `pdt-cli==X` pin in the script header of each app's
run.py moves to the newest release. That pin is the pdt the app runs with, on
this computer and when deployed, so a deploy ships it. An app with no run.py
runs the pdt that deploys it, and needs only the pdt upgrade.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

from pdt import config, console
from pdt.config import ConfigError

PYPI_URL = "https://pypi.org/pypi/pdt-cli/json"
WINGET_ID = "AutoIDM.pdt"
CLONE_BRANCH = "main"
# The PEP 723 block at the top of run.py, and a pdt-cli pin inside it.
SCRIPT_HEADER = re.compile(r"^# /// script[ \t]*\r?$.*?^# ///[ \t]*\r?$", re.M | re.S)
PIN = re.compile(r"(pdt-cli(?:\[[^\]]*\])?\s*==\s*)([^\s\"',;\]]+)")


class UpgradeError(Exception):
    pass


def newest() -> str:
    """The newest pdt-cli release on PyPI."""
    try:
        with urllib.request.urlopen(PYPI_URL, timeout=30) as response:
            return json.load(response)["info"]["version"]
    except (urllib.error.URLError, OSError, ValueError, KeyError) as e:
        reason = getattr(e, "reason", e)
        raise UpgradeError(f"could not read the newest pdt release from {PYPI_URL}: {reason}")


def release(version: str) -> tuple[int, ...]:
    """The numbers at the start of `version`, to compare: 0.1.10 is newer than 0.1.9."""
    match = re.match(r"\d+(?:\.\d+)*", version)
    return tuple(int(part) for part in match.group().split(".")) if match else ()


def install_kind() -> tuple[str, Path]:
    """How this pdt was installed, and where: ("clone", the clone's folder), ("winget", the
    tool's folder), ("uv", the tool's folder), or ("other", the Python environment)."""
    root = Path(__file__).resolve().parent.parent.parent
    if (root / ".git").exists() and (root / "pyproject.toml").is_file():
        return "clone", root
    prefix = Path(sys.prefix).resolve()
    tools = uv_tool_dir()
    if tools is None or not prefix.is_relative_to(tools):
        return "other", prefix
    local = os.environ.get("LOCALAPPDATA", "")
    if local != "" and tools.is_relative_to(Path(local).resolve() / "AutoIDM" / "pdt"):
        return "winget", prefix
    return "uv", prefix


def uv_tool_dir() -> Path | None:
    """The folder uv keeps its tools in. The winget launcher points UV_TOOL_DIR at its own."""
    try:
        done = subprocess.run(["uv", "tool", "dir"], capture_output=True, text=True)
    except OSError:
        return None
    folder = done.stdout.strip()
    return Path(folder).resolve() if done.returncode == 0 and folder != "" else None


def requirement(tool: Path) -> str:
    """`pdt-cli`, with the extras the uv tool was installed with, such as `pdt-cli[apps]`."""
    try:
        receipt = tomllib.loads((tool / "uv-receipt.toml").read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return "pdt-cli"
    for wanted in receipt.get("tool", {}).get("requirements", []):
        if wanted.get("name") == "pdt-cli" and wanted.get("extras"):
            return f"pdt-cli[{','.join(wanted['extras'])}]"
    return "pdt-cli"


def upgrade_command(kind: str, tool: Path, latest: str) -> list[str]:
    if kind == "winget":
        return ["winget", "upgrade", "--id", WINGET_ID, "--exact",
                "--accept-source-agreements", "--accept-package-agreements"]
    return ["uv", "tool", "install", f"{requirement(tool)}>={latest}"]


def upgrade_tool(current: str, latest: str) -> bool:
    """Upgrade this pdt to `latest`. True when it is upgraded, or will be once this command
    ends."""
    kind, where = install_kind()
    if kind == "clone":
        return pull_clone(where)
    if release(current) >= release(latest):
        console.done(f"pdt {console.value(current)} is the newest release.")
        return False
    if kind == "other":
        console.note(f"pdt runs from {console.value(where)}, which is not a uv tool or winget "
                     f"install, so pdt upgrade leaves it at {console.value(current)}.")
        return False
    command = upgrade_command(kind, where, latest)
    how = "winget" if kind == "winget" else "uv"
    if sys.platform == "win32":
        after_exit(command)
        console.done(f"pdt {console.value(current)} moves to {console.value(latest)} through "
                     f"{console.value(how)} in a new window, once this command ends.")
        return True
    console.step(f"Upgrading pdt {console.value(current)} to {console.value(latest)} "
                 f"through {console.value(how)}: {console.value(' '.join(command))}")
    if subprocess.run(command).returncode != 0:
        raise UpgradeError(f"{' '.join(command)} failed; the lines above say why")
    console.done(f"pdt is now {console.value(latest)}.")
    return True


def pull_clone(root: Path) -> bool:
    """`git pull --ff-only` in the clone pdt runs from, when it is on main with no changes."""
    try:
        branch = git(root, "rev-parse", "--abbrev-ref", "HEAD")
        changes = git(root, "status", "--porcelain", "--untracked-files=no")
    except (OSError, subprocess.CalledProcessError):
        console.note(f"pdt runs from the clone at {console.value(root)}, and git could not "
                     "read it, so pdt upgrade leaves it as it is.")
        return False
    if branch != CLONE_BRANCH:
        console.note(f"pdt runs from the clone at {console.value(root)}, which is on branch "
                     f"{console.value(branch)}, so pdt upgrade leaves it as it is.")
        return False
    if changes != "":
        console.note(f"pdt runs from the clone at {console.value(root)}, which has changes "
                     "that are not committed, so pdt upgrade leaves it as it is.")
        return False
    command = ["git", "-C", str(root), "pull", "--ff-only"]
    console.step(f"Updating the pdt clone at {console.value(root)}: "
                 f"{console.value('git pull --ff-only')}")
    if subprocess.run(command).returncode != 0:
        raise UpgradeError(f"git pull --ff-only in {root} failed; the lines above say why")
    return True


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True,
                          check=True).stdout.strip()


# Each pdt process of this command: this one, then each parent named pdt (uv's pdt.exe,
# and the winget launcher's two). A program cannot replace pdt until all of them end.
ANCESTORS = """\
$id = {pid}
while ($true) {{
  $id
  $parent = (Get-CimInstance Win32_Process -Filter "ProcessId=$id").ParentProcessId
  $process = Get-Process -Id $parent -ErrorAction SilentlyContinue
  if (-not $process -or $process.ProcessName -ne 'pdt') {{ break }}
  $id = $parent
}}
"""

AFTER_EXIT = """\
Write-Host 'Waiting for pdt to close...'
foreach ($id in @({ids})) {{ Wait-Process -Id $id -ErrorAction SilentlyContinue }}
& {command}
if ($LASTEXITCODE -eq 0) {{ Write-Host 'pdt is upgraded.' }}
else {{ Write-Host 'The pdt upgrade failed; the lines above say why.' }}
Read-Host 'Press Enter to close this window'
"""


def powershell_quote(word: str) -> str:
    return "'" + word.replace("'", "''") + "'"


def after_exit(command: list[str]) -> None:
    """Run `command` in a new PowerShell window once each pdt process of this command has
    ended."""
    shell = shutil.which("powershell") or "powershell"
    found = subprocess.run(
        [shell, "-NoLogo", "-NoProfile", "-Command", ANCESTORS.format(pid=os.getpid())],
        capture_output=True, text=True)
    ids = [line.strip() for line in found.stdout.splitlines() if line.strip().isdigit()]
    script = AFTER_EXIT.format(ids=",".join(ids or [str(os.getpid())]),
                               command=" ".join(powershell_quote(word) for word in command))
    window = [shell, "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script]
    new_console = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
    try:
        # Out of uv's job object, so the window outlives the pdt.exe that started this one.
        subprocess.Popen(window, creationflags=new_console
                         | getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0))
    except OSError:
        subprocess.Popen(window, creationflags=new_console)


def raise_pin(text: str, latest: str) -> tuple[str, str | None]:
    """`text` with each pdt-cli==X pin in its script header older than `latest` raised to
    it, and the pin it found (None for no pin)."""
    header = SCRIPT_HEADER.search(text)
    if header is None:
        return text, None
    pins = PIN.findall(header.group())
    if not pins:
        return text, None
    found = pins[0][1]

    def to_latest(match: re.Match) -> str:
        if release(match.group(2)) >= release(latest):
            return match.group()
        return match.group(1) + latest

    new_header = PIN.sub(to_latest, header.group())
    return text[:header.start()] + new_header + text[header.end():], found


def raise_pins(latest: str) -> list[str]:
    """Raise the pdt-cli pin of each app in the project to `latest`, print one line per app,
    and return the apps whose pin moved."""
    project = config.find_project()
    names = config.app_folders()
    changed = []
    if names:
        console.heading("Apps:")
    for name in names:
        run_py = project / name / "run.py"
        if not run_py.is_file():
            console.bullet(f"{console.value(name)}  runs the pdt that deploys it")
            continue
        # newline="" keeps a Windows file's line endings as they are.
        with open(run_py, encoding="utf-8", newline="") as file:
            text = file.read()
        new_text, found = raise_pin(text, latest)
        if found is None:
            console.bullet(f"{console.value(name)}  run.py pins no pdt-cli version")
        elif new_text == text:
            console.bullet(f"{console.value(name)}  pdt-cli {console.value(found)} "
                           "(already the newest)")
        else:
            with open(run_py, "w", encoding="utf-8", newline="") as file:
                file.write(new_text)
            console.bullet(f"{console.value(name)}  pdt-cli {console.value(found)} "
                           f"to {console.value(latest)}")
            changed.append(name)
    return changed


def main() -> int:
    from pdt import __version__
    try:
        project = config.find_project()
    except ConfigError:
        project = None
    try:
        latest = newest()
        console.field("Newest pdt release", latest)
        upgraded = upgrade_tool(__version__, latest)
        changed = raise_pins(latest) if project is not None else []
    except UpgradeError as e:
        console.error(console.escape(str(e)))
        return 1
    if project is None:
        return 0
    redeploy = [name for name in config.app_folders() if config.is_deployed(name) and (
        name in changed or (upgraded and not (project / name / "run.py").is_file()))]
    if changed:
        console.done(f"Raised the pdt-cli pin in {console.value(len(changed))} app(s).")
    if redeploy:
        console.next_steps([(f"pdt deploy {name}", "ship the new pdt to the deployed job")
                            for name in redeploy])
    return 0
