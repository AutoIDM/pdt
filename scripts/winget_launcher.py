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
from pathlib import Path

VERSION = "0.0.0"
NEEDS_UV = ("pdt needs uv. Install it with: winget install astral-sh.uv\n"
            "Then open a new PowerShell window and run pdt again.")


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
    if not installed.is_dir():
        sys.stderr.write(f"Installing pdt {VERSION}. The first install downloads Python and the pdt libraries.\n")
        if subprocess.run([uv, "tool", "install", f"pdt-cli=={VERSION}"], env=env).returncode != 0:
            return 1
    return subprocess.run([str(home / "bin" / "pdt.exe"), *args]).returncode


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
