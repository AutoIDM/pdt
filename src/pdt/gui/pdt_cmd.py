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
import time
from datetime import UTC, datetime
from pathlib import Path

import pdt
from pdt import config
from pdt.gui import timing


def run_pdt(*args: str, timeout: int = 900) -> tuple[int, str]:
    """The exit code and output of `pdt <args>`; stderr is added when it failed."""
    env = dict(os.environ, PDT_PROJECT=str(config.find_project()), NO_COLOR="1",
               PYTHONPATH=str(Path(pdt.__file__).resolve().parent.parent))
    started = datetime.now(UTC)
    clock = time.monotonic()
    try:
        proc = subprocess.run([sys.executable, "-m", "pdt.cli", *args], env=env,
                              stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              timeout=timeout)
    except subprocess.TimeoutExpired:
        timing.record("pdt", timing.command_name(args), " ".join(args), started,
                      time.monotonic() - clock, False)
        return 1, f"pdt {' '.join(args)} did not finish within {timeout} seconds"
    # `pdt logs` of a failed run exits 1 and still answers; a JSON answer counts as ok.
    answered = proc.returncode == 0 or ("--json" in args and last_json(proc.stdout) is not None)
    timing.record("pdt", timing.command_name(args), " ".join(args), started,
                  time.monotonic() - clock, answered)
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
