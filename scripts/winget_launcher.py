"""The pdt.exe that the winget zip and the MSI install.

It installs pdt-cli at VERSION as a uv tool when that version is not there yet,
then runs the real pdt with the same arguments.
The tool and its pdt.exe go under %LOCALAPPDATA%\\AutoIDM\\pdt, a folder that
is not on the PATH, so this launcher is the pdt that runs and a winget upgrade
or a newer MSI takes effect. A `uv tool install pdt-cli` keeps its own copy in
uv's folders.
The release workflow replaces VERSION with the version being released.
Standard library only, so PyInstaller has nothing else to bundle.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

VERSION = "0.0.0"
NEEDS_UV = ("pdt needs uv. Install it with: winget install astral-sh.uv\n"
            "Then open a new PowerShell window and run pdt again.")
TICK = 0.25
PHASES = (
    (b"Downloading cpython", "downloading Python"),
    (b"Downloaded cpython", "finding the pdt libraries"),
    (b"Resolved ", "downloading the pdt libraries"),
    (b"Prepared ", "installing"),
)


def phase(output: list[bytes]) -> str:
    for line in reversed(output):
        for prefix, name in PHASES:
            if line.lstrip().startswith(prefix):
                return name
    return "finding the pdt libraries"


def install(uv: str, env: dict[str, str]) -> bool:
    started = time.monotonic()
    proc = subprocess.Popen([uv, "tool", "install", f"pdt-cli=={VERSION}"], env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    output: list[bytes] = []

    def read() -> None:
        for line in proc.stdout:
            output.append(line)

    reader = threading.Thread(target=read)
    reader.start()
    tty = sys.stderr.isatty()
    if not tty:
        sys.stderr.write(f"Setting up pdt {VERSION}: started\n")
    shown = ""
    while reader.is_alive():
        if tty:
            line = f"Setting up pdt {VERSION}: {phase(output)} ({int(time.monotonic() - started)}s)"
            sys.stderr.write("\r" + line.ljust(len(shown)))
            sys.stderr.flush()
            shown = line
        reader.join(TICK)
    ok = proc.wait() == 0
    state = "done" if ok else "failed"
    end = f"Setting up pdt {VERSION}: {state} ({int(time.monotonic() - started)}s)"
    if tty:
        sys.stderr.write("\r")
    sys.stderr.write(end.ljust(len(shown)) + "\n")
    sys.stderr.flush()
    if not ok:
        sys.stderr.buffer.write(b"".join(output))
        sys.stderr.buffer.flush()
    return ok


def main(args: list[str]) -> int:
    bundled = Path(sys.executable).with_name("uv.exe")
    uv = str(bundled) if bundled.is_file() else shutil.which("uv")
    if uv is None:
        sys.stderr.write(NEEDS_UV + "\n")
        return 1
    home = Path(os.environ["LOCALAPPDATA"]) / "AutoIDM" / "pdt"
    env = {
        **os.environ,
        "UV_TOOL_DIR": str(home / "tools"),
        "UV_TOOL_BIN_DIR": str(home / "bin"),
    }
    installed = home / "tools" / "pdt-cli" / "Lib" / "site-packages" / f"pdt_cli-{VERSION}.dist-info"
    if not installed.is_dir() and not install(uv, env):
        return 1
    return subprocess.run([str(home / "bin" / "pdt.exe"), *args]).returncode


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
