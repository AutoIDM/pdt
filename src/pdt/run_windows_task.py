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
an app with no run.py, `uv run --script run_powershell.py <app dir>`) inside
`lifecycle.py`, which adds the job's lifecycle events to the log (with
PDT_TRIGGER=manual when it finds MANUAL_START), writes
both streams to
<logs folder>\\<UTC start>.log, each line after the UTC time it arrived
(LINE_TIME), ends the file with `pdt: exit N` (the marker `pdt runs` reads),
and exits with the child's exit code so `Get-ScheduledTaskInfo` shows it.

It needs nothing beyond the standard library and never prints: nobody watches
a scheduled task. When uv cannot be started the log holds the reason and
`pdt: exit 127`.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

USAGE = "usage: run_windows_task.py <app dir> <logs folder> <storage url>"
EXIT_MARKER = "pdt: exit "
MISSING_UV = 127
# `pdt run APP --deployed` writes this file in the logs folder before it starts the task,
# since Task Scheduler passes nothing to one run. A run that finds it, at most
# MANUAL_START_SECONDS old, is manual.
MANUAL_START = "manual-start"
SECRETS_FILE = "secrets.json"
# The Python of every cloud image (deploy_common.BASE_IMAGE); pdt.windows_uv.PYTHON too.
PYTHON = "3.12"
MANUAL_START_SECONDS = 300
LINE_TIME = re.compile(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z) ")
DRAIN_SECONDS = 2


def timed(raw: bytes) -> bytes:
    moment = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    return f"{moment.replace('+00:00', 'Z')} ".encode() + raw.rstrip(b"\r\n") + b"\n"


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
    secrets = folder.parent / SECRETS_FILE
    if secrets.is_file():
        env["PDT_ENV_JSON"] = secrets.read_text(encoding="utf-8-sig")
    env.setdefault("UV_CACHE_DIR", str(folder.parent.parent / "uv-cache"))
    env["UV_PYTHON"] = PYTHON
    marker = folder / MANUAL_START
    if marker.is_file():
        if time.time() - marker.stat().st_mtime < MANUAL_START_SECONDS:
            env["PDT_TRIGGER"] = "manual"
        marker.unlink(missing_ok=True)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    # Python holds stdout written to a file or pipe until a buffer fills, so a print()
    # line would land after a later stderr line. The cloud images set the same.
    env.setdefault("PYTHONUNBUFFERED", "1")
    uv = os.environ.get("UV") or "uv"
    if (Path(app_dir) / "run.py").is_file():
        command = [uv, "run", "--script", "run.py"]
    else:
        command = [uv, "run", "--script", str(Path(__file__).with_name("run_powershell.py")), app_dir]
    command = [uv, "run", "--script", str(Path(__file__).with_name("lifecycle.py")), "--", *command]
    with log.open("wb") as out:
        try:
            child = subprocess.Popen(
                command, cwd=app_dir, env=env,
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        except OSError as exc:
            out.write(timed(f"{uv} could not be started: {exc}".encode()))
            code = MISSING_UV
        else:
            lock = threading.Lock()
            ended = []
            rest = [b""]

            def copy() -> None:
                while True:
                    chunk = child.stdout.read1()
                    with lock:
                        if ended:
                            return
                        if chunk == b"":
                            chunk = b"\n" if rest[0] else b""
                            ended.append(True)
                        *complete, rest[0] = (rest[0] + chunk).split(b"\n")
                        for raw in complete:
                            out.write(timed(raw))
                        out.flush()
            reader = threading.Thread(target=copy, daemon=True)
            reader.start()
            try:
                code = child.wait()
            except BaseException:
                child.kill()
                child.wait()
                raise
            # A process the app started in the background can hold the pipe open after the
            # app exits, so the run ends without waiting for it.
            reader.join(DRAIN_SECONDS)
            with lock:
                ended.append(True)
                if rest[0]:
                    out.write(timed(rest[0]))
        out.write(timed(f"{EXIT_MARKER}{code}".encode()))
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
