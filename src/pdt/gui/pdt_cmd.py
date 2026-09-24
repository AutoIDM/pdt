"""Run a pdt command for the GUI, in a child process, the way a user would.

The child is `python -m pdt.cli` with this interpreter, which is the
`uv run --script` environment of gui_server.py, so it sees the same pdt
package. PDT_PROJECT names the project, NO_COLOR keeps the output plain.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pdt
from pdt import config


def run_pdt(*args: str, timeout: int = 900) -> tuple[int, str]:
    """The exit code and output of `pdt <args>`; stderr is added when it failed."""
    env = dict(os.environ, PDT_PROJECT=str(config.find_project()), NO_COLOR="1",
               PYTHONPATH=str(Path(pdt.__file__).resolve().parent.parent))
    try:
        proc = subprocess.run([sys.executable, "-m", "pdt.cli", *args], env=env,
                              stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              timeout=timeout)
    except subprocess.TimeoutExpired:
        return 1, f"pdt {' '.join(args)} did not finish within {timeout} seconds"
    output = proc.stdout
    if proc.returncode != 0 and proc.stderr.strip():
        output += ("\n" if output and not output.endswith("\n") else "") + proc.stderr
    return proc.returncode, output


def last_json(output: str):
    """The JSON value on the last line of a `--json` command's output, or None."""
    lines = output.strip().splitlines()
    if not lines:
        return None
    try:
        return json.loads(lines[-1])
    except ValueError:
        return None
