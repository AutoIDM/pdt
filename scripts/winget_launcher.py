"""The pdt.exe that winget installs.

It installs pdt-cli at VERSION as a uv tool, which returns at once when that
version is already there, then runs the real pdt with the same arguments.
The release workflow replaces VERSION with the version being released.
Standard library only, so PyInstaller has nothing else to bundle.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

VERSION = "0.0.0"
NEEDS_UV = "pdt needs uv. Install it with: winget install astral-sh.uv"


def quiet(command: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(command, capture_output=True, text=True)


def main(args: list[str]) -> int:
    uv = shutil.which("uv")
    if uv is None:
        sys.stderr.write(NEEDS_UV + "\n")
        return 1
    for step in (["tool", "install", f"pdt-cli=={VERSION}"], ["tool", "dir", "--bin"]):
        done = quiet([uv, *step])
        if done.returncode != 0:
            sys.stderr.write(done.stderr)
            return 1
    pdt = Path(done.stdout.strip()) / "pdt.exe"
    return subprocess.run([str(pdt), *args]).returncode


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
