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

`emit` gives each event to every entry of the app's `alerts:` list whose
`events` patterns match the event name, through that entry's type in
TRANSPORTS. The one type, console, prints the event as a `[event]` line; a
later transport (email, a webhook, the GUI history) adds its own entry to
TRANSPORTS.

`job:` names are events of a scheduled job's run, `app:` names are events
of the app itself. The names pdt emits are the constants below; `emit`
takes any name, so an app can emit its own. The standard parameters are
project, app, provider, and run_id where a run exists.

`run_job` runs an app's command between `job:run-started` and
`job:run-completed` or `job:run-failed`, and gives the command PDT_RUN_ID so
the app's `runs/` folder ends with the run_id of the events. `pdt run` calls it,
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
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pdt import config, console
from pdt.utils import storage

JOB_RUN_STARTED = "job:run-started"
JOB_RUN_COMPLETED = "job:run-completed"
JOB_RUN_FAILED = "job:run-failed"
JOB_PAUSED = "job:paused"
JOB_UNPAUSED = "job:unpaused"
APP_DEPLOY_STARTED = "app:deploy-started"
APP_DEPLOY_COMPLETED = "app:deploy-completed"
APP_DEPLOY_FAILED = "app:deploy-failed"


@dataclass(frozen=True)
class Event:
    name: str
    at: datetime
    params: dict[str, object]


def print_event(event: Event, alert: dict) -> None:
    fields = " ".join(f"{key}={value}" for key, value in event.params.items())
    console.event(f"{event.name} at={event.at:%Y-%m-%dT%H:%M:%SZ} {fields}")


TRANSPORTS: dict[str, Callable[[Event, dict], None]] = {"console": print_event}
DEFAULT_ALERTS = [{"type": "console", "events": ["*"]}]


def parse_alerts(alerts: object, where: str) -> list[dict]:
    known = ", ".join(TRANSPORTS)
    if not isinstance(alerts, list):
        raise config.ConfigError(f"{where}: alerts must be a list of entries, each with a type")
    parsed = []
    for alert in alerts:
        if not isinstance(alert, dict) or "type" not in alert:
            raise config.ConfigError(f"{where}: every alerts entry needs a type, one of: {known}")
        if alert["type"] not in TRANSPORTS:
            raise config.ConfigError(
                f"{where}: alerts type {alert['type']!r} is not known; use one of: {known}")
        include, exclude = event_patterns(alert, where)
        parsed.append({**alert, "include": include, "exclude": exclude})
    return parsed


def event_patterns(alert: dict, where: str) -> tuple[list[str], list[str]]:
    """The entry's include and exclude patterns: `!` marks an exclude, and no `events` key
    means every event."""
    events = alert.get("events", ["*"])
    if not (isinstance(events, list) and all(isinstance(e, str) for e in events)):
        raise config.ConfigError(
            f"{where}: alerts events must be a list of event name patterns in quotes")
    return ([p for p in events if not p.startswith("!")],
            [p[1:] for p in events if p.startswith("!")])


def matches(alert: dict, name: str) -> bool:
    return (any(fnmatchcase(name, p) for p in alert["include"])
            and not any(fnmatchcase(name, p) for p in alert["exclude"]))


def alerts_for(app: object) -> list[dict]:
    """The app's alerts, or the project's when no app config loads, or the default."""
    if app is not None:
        try:
            return config.merged_app(str(app))["alerts"]
        except config.ConfigError:
            pass
    try:
        root_cfg = config.load_yaml(config.find_project() / config.PROJECT_FILE)
        return parse_alerts(root_cfg.get("alerts", DEFAULT_ALERTS), config.PROJECT_FILE)
    except config.ConfigError:
        return parse_alerts(DEFAULT_ALERTS, config.PROJECT_FILE)


def emit(name: str, **params: object) -> Event:
    event = Event(name, datetime.now(timezone.utc), params)
    for alert in alerts_for(params.get("app")):
        if matches(alert, name):
            TRANSPORTS[alert["type"]](event, alert)
    return event


def app_params(app: str, provider: str) -> dict[str, object]:
    return {"project": config.find_project().name, "app": app, "provider": provider}


def run_job(app: str, provider: str, command: list[str], cwd: Path,
            env: dict[str, str] | None = None) -> int:
    params = {**app_params(app, provider), "run_id": storage.RUN_ID}
    emit(JOB_RUN_STARTED, **params)
    code = subprocess.run(command, cwd=cwd, env={**(env or os.environ),
                                                 "PDT_RUN_ID": storage.RUN_ID}).returncode
    emit(JOB_RUN_COMPLETED if code == 0 else JOB_RUN_FAILED, **params, exit_code=code,
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
