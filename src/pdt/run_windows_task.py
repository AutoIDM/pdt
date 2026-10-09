#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Run an app once for its Windows scheduled task and keep the output.

usage: uv run --script run_windows_task.py <app dir> <logs folder> <storage url>

Task Scheduler discards stdout and stderr, so the task `pdt-<app>` runs this
script instead of run.py. It sets PDT_STORAGE_URL and PDT_RUN_ID (the UTC start,
so the app's `runs/` folder is named after the run `pdt runs` lists), runs
`uv run --script run.py` in the app folder with the uv that started it (or, for
an app with no run.py, `uv run --script run_powershell.py <app dir>`), writes
both streams to
<logs folder>\\<UTC start>.log, ends the file with `pdt: exit N` (the marker
`pdt runs` reads), and exits with the child's exit code so
`Get-ScheduledTaskInfo` shows it.

It needs nothing beyond the standard library and never prints: nobody watches
a scheduled task. When uv cannot be started the log holds the reason and
`pdt: exit 127`.
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

USAGE = "usage: run_windows_task.py <app dir> <logs folder> <storage url>"
EXIT_MARKER = "pdt: exit "
MISSING_UV = 127


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        sys.stderr.write(USAGE + "\n")
        return 2
    app_dir, logs, storage_url = argv
    folder = Path(logs)
    folder.mkdir(parents=True, exist_ok=True)
    stamp = f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
    log = folder / f"{stamp}.log"
    env = {**os.environ, "PDT_STORAGE_URL": storage_url, "PDT_RUN_ID": stamp}
    env.setdefault("PYTHONIOENCODING", "utf-8")
    # Python holds stdout written to a file or pipe until a buffer fills, so a print()
    # line would land after a later stderr line. The cloud images set the same.
    env.setdefault("PYTHONUNBUFFERED", "1")
    uv = os.environ.get("UV") or "uv"
    if (Path(app_dir) / "run.py").is_file():
        command = [uv, "run", "--script", "run.py"]
    else:
        command = [uv, "run", "--script", str(Path(__file__).with_name("run_powershell.py")), app_dir]
    with log.open("wb") as out:
        try:
            code = subprocess.run(
                command, cwd=app_dir, env=env,
                stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT).returncode
        except OSError as exc:
            out.write(f"{uv} could not be started: {exc}\n".encode())
            code = MISSING_UV
        out.write(f"{EXIT_MARKER}{code}\n".encode())
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
