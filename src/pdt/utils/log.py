"""One-line-per-event logging shared by the apps in this repo.

Levels are Cloud Logging severities: debug, info, warning, error.
Format is human-readable text locally and JSON on Cloud Run (which
sets CLOUD_RUN_JOB). LOG_FORMAT=json|text forces either mode.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from typing import NoReturn

LEVEL_COLOURS = {
    "DEBUG": "\x1b[2m",
    "INFO": "\x1b[32m",
    "WARNING": "\x1b[33m",
    "ERROR": "\x1b[1;31m",
}


def log(level: str, msg: str, **kv) -> None:
    mode = os.environ.get("LOG_FORMAT", "").strip()
    if mode == "":
        mode = "json" if os.environ.get("CLOUD_RUN_JOB", "") != "" else "text"
    if mode == "json":
        rec = {"severity": level.upper(), "message": msg, **kv}
        print(json.dumps(rec, default=str), flush=True)
    else:
        timestamp = f"{datetime.now():%H:%M:%S}"
        level_field = f"{level.upper():<7}"
        if sys.stdout.isatty():
            colour = LEVEL_COLOURS.get(level.upper(), "")
            timestamp = f"\x1b[2m{timestamp}\x1b[0m"
            if colour != "":
                level_field = f"{colour}{level_field}\x1b[0m"
        line = f"{timestamp} {level_field} {msg}"
        pairs = " ".join(f"{k}={v}" for k, v in kv.items())
        if pairs != "":
            line = f"{line}  {pairs}"
        print(line, flush=True)


def die(code: int, msg: str, **kv) -> NoReturn:
    log("error", msg, **kv)
    sys.exit(code)
