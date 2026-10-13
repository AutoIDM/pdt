#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "pyyaml",
#     "python-dotenv",
#     "rich",
# ]
# ///
"""Lifecycle events: a name, the UTC time, and a few parameters.

usage: lifecycle.py -- <command...>

`emit` gives each event to every function in HANDLERS. The one handler
prints the event as a `[event]` line; a later transport (email, a webhook,
the GUI history) appends its own function to HANDLERS.

`job:` names are events of a scheduled job's run, `app:` names are events
of the app itself. The names pdt emits are the constants below; `emit`
takes any name, so an app can emit its own. The standard parameters are
project, app, provider, and run_id where a run exists.

`run_job` runs an app's command between `job:run-started` and
`job:completed` or `job:failed`, and gives the command PDT_RUN_ID so the
app's `runs/` folder ends with the run_id of the events. `pdt run` calls it,
and the Windows task and every cloud image (a copy at /opt/pdt-lifecycle,
deploy_common.LIFECYCLE_MODULES) run this file as a script around the app's
command, from the app folder, so the events land in the run's log.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pdt import config, console
from pdt.utils import storage

JOB_RUN_STARTED = "job:run-started"
JOB_COMPLETED = "job:completed"
JOB_FAILED = "job:failed"
JOB_PAUSED = "job:paused"
JOB_UNPAUSED = "job:unpaused"
APP_DEPLOY_START = "app:deploy-start"
APP_DEPLOY_COMPLETED = "app:deploy-completed"
APP_DEPLOY_FAILED = "app:deploy-failed"


@dataclass(frozen=True)
class Event:
    name: str
    at: datetime
    params: dict[str, object]


def print_event(event: Event) -> None:
    fields = " ".join(f"{key}={value}" for key, value in event.params.items())
    console.event(f"{event.name} at={event.at:%Y-%m-%dT%H:%M:%SZ} {fields}")


HANDLERS: list[Callable[[Event], None]] = [print_event]


def emit(name: str, **params: object) -> Event:
    event = Event(name, datetime.now(timezone.utc), params)
    for handler in HANDLERS:
        handler(event)
    return event


def app_params(app: str, provider: str) -> dict[str, object]:
    return {"project": config.find_project().name, "app": app, "provider": provider}


def run_job(app: str, provider: str, command: list[str], cwd: Path,
            env: dict[str, str] | None = None) -> int:
    params = {**app_params(app, provider), "run_id": storage.RUN_ID}
    emit(JOB_RUN_STARTED, **params)
    code = subprocess.run(command, cwd=cwd, env={**(env or os.environ),
                                                 "PDT_RUN_ID": storage.RUN_ID}).returncode
    emit(JOB_COMPLETED if code == 0 else JOB_FAILED, **params, exit_code=code,
         state=f"runs/*-{storage.RUN_ID}/")
    return code


def main(argv: list[str]) -> int:
    if argv[:1] != ["--"] or len(argv) < 2:
        sys.stderr.write("usage: lifecycle.py -- <command...>\n")
        return 2
    app = Path.cwd().name
    try:
        provider = config.merged_app(app)["platform"].get("provider", "")
    except config.ConfigError:
        provider = ""
    return run_job(app, provider, argv[1:], Path.cwd())


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
