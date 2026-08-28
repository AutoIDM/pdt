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


def log(level: str, msg: str, **kv) -> None:
    mode = os.environ.get("LOG_FORMAT", "").strip()
    if mode == "":
        mode = "json" if os.environ.get("CLOUD_RUN_JOB", "") != "" else "text"
    if mode == "json":
        rec = {"severity": level.upper(), "message": msg, **kv}
        print(json.dumps(rec, default=str), flush=True)
    else:
        line = f"{datetime.now():%H:%M:%S} {level.upper():<7} {msg}"
        pairs = " ".join(f"{k}={v}" for k, v in kv.items())
        if pairs != "":
            line = f"{line}  {pairs}"
        print(line, flush=True)


def die(code: int, msg: str, **kv) -> NoReturn:
    log("error", msg, **kv)
    sys.exit(code)
