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

`emit` gives each event to every entry of the app's `alerts:` list that
receives it, once per entry and in config order, through the entry's type
in TRANSPORTS. An entry receives an event that matches at least one name in
its `events` and no name in its `exclude-events`; a name may hold a `*`
glob, and `all` is every event. An event that no entry receives this way is
unhandled, and goes to each entry with `unhandled` in its `events` (its
`exclude-events` still apply). When no entry names `unhandled` in `events`
or `exclude-events`, the list ends with the implicit entry
{type: console, events: [unhandled]}, so with no `alerts:` the console
prints every event, marked unhandled. `alerts: false` is an empty list with no
implicit entry, so no entry receives any event.

A Transport is a name, the extra entry keys it allows, and
`deliver(event, entry, unhandled)`, where `unhandled` says the entry got the
event through `unhandled`. The one type, console, prints a `[event]` line; a
later transport (email, a webhook, the GUI history) adds its own Transport.
`deliver` must not change the event. The console prints at once; every
other transport delivers on a thread of its own entry, one event at a time
in emit order, so `emit` returns at once. `flush`, which `run_job` and the
end of every pdt command call, waits for every open delivery for at most
FLUSH_SECONDS in total. An event that a transport did not deliver in that
time, or whose `deliver` raised (with a warning on stderr that names the
error), prints on the console marked `not delivered to <type>`. A
transport never changes the exit code of a run, a deploy, or a destroy, and pdt emits
no event about a failed delivery.

`job:` names are events of a scheduled job's run, `app:` names are events
of the app itself. The names pdt emits are the constants below; `emit`
takes any name, so an app can emit its own. The standard parameters are
project, app, and provider (the configured one), and for a `job:run-*` event
run_id, deployed (false for a local `pdt run`), and trigger (`schedule` or
`manual`). `app:secrets-updated` adds action and count, and no env var name or value.

`run_job` runs an app's command between `job:run-started` and
`job:run-completed`, `job:run-failed`, or `job:run-cancelled`, and gives the
command PDT_RUN_ID so the app's `runs/` folder ends with the run_id of the
events. A SIGINT or SIGTERM to `run_job` goes on to the command, and the
run ends with `job:run-cancelled` once the command exits. `pdt run` calls
it, and the Windows task and every cloud image (a copy at
/opt/pdt-lifecycle, deploy_common.LIFECYCLE_MODULES) run this file as a
script around the app's command, from the app folder, so the events land in
the run's log. A SIGKILL, a Windows task that Task Scheduler ends, or a
power loss stops pdt at once, so no event fires.
"""

from __future__ import annotations

import os
import queue
import signal
import subprocess
import sys
import threading
import time
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
JOB_RUN_CANCELLED = "job:run-cancelled"
JOB_PAUSED = "job:paused"
JOB_UNPAUSED = "job:unpaused"
APP_DEPLOY_STARTED = "app:deploy-started"
APP_DEPLOY_COMPLETED = "app:deploy-completed"
APP_DEPLOY_FAILED = "app:deploy-failed"
APP_DEPLOY_CANCELLED = "app:deploy-cancelled"
APP_DESTROY_STARTED = "app:destroy-started"
APP_DESTROY_COMPLETED = "app:destroy-completed"
APP_DESTROY_FAILED = "app:destroy-failed"
APP_DESTROY_CANCELLED = "app:destroy-cancelled"
APP_SECRETS_UPDATED = "app:secrets-updated"
# Set to `manual` for one execution by the pdt command that starts a deployed run.
TRIGGER_ENV = "PDT_TRIGGER"


@dataclass(frozen=True)
class Event:
    name: str
    at: datetime
    params: dict[str, object]


def event_text(event: Event) -> str:
    fields = " ".join(f"{key}={value}" for key, value in event.params.items())
    return f"{event.name} at={event.at:%Y-%m-%dT%H:%M:%SZ} {fields}"


def print_event(event: Event, entry: dict, unhandled: bool) -> None:
    console.event(event_text(event), UNHANDLED if unhandled else "", "red")


@dataclass(frozen=True)
class Transport:
    name: str
    keys: frozenset[str]
    deliver: Callable[[Event, dict, bool], None]


ENTRY_KEYS = ("type", "events", "exclude-events")
ALL = "all"
UNHANDLED = "unhandled"
TRANSPORTS: dict[str, Transport] = {"console": Transport("console", frozenset(), print_event)}


def parse_alerts(alerts: object, where: str) -> list[dict]:
    """The checked entries, with `all` as `*`, and the implicit unhandled entry at the end
    when no entry names `unhandled`; none for `alerts: false`."""
    known = ", ".join(TRANSPORTS)
    if alerts is False:
        return []
    if not isinstance(alerts, list):
        raise config.ConfigError(
            f"{where}: alerts must be a list of entries, each with a type, or false")
    parsed = []
    for alert in alerts:
        if not isinstance(alert, dict) or "type" not in alert:
            raise config.ConfigError(f"{where}: every alerts entry needs a type, one of: {known}")
        if alert["type"] not in TRANSPORTS:
            raise config.ConfigError(
                f"{where}: alerts type {alert['type']!r} is not known; use one of: {known}")
        allowed = [*ENTRY_KEYS, *sorted(TRANSPORTS[alert["type"]].keys)]
        for key in alert:
            if key not in allowed:
                raise config.ConfigError(
                    f"{where}: alerts: unknown key {key!r}; a {alert['type']} entry takes: "
                    + ", ".join(allowed))
        if "events" not in alert and "exclude-events" not in alert:
            raise config.ConfigError(
                f"{where}: the {alert['type']} alerts entry needs events, for example "
                "events: [all]")
        parsed.append({**alert, "events": event_patterns(alert, "events", where),
                       "exclude-events": event_patterns(alert, "exclude-events", where)})
    if not any(UNHANDLED in alert["events"] + alert["exclude-events"] for alert in parsed):
        parsed.append({"type": "console", "events": [UNHANDLED], "exclude-events": []})
    return parsed


def event_patterns(alert: dict, key: str, where: str) -> list[str]:
    names = alert.get(key, [])
    if not (isinstance(names, list) and all(isinstance(n, str) for n in names)):
        raise config.ConfigError(
            f"{where}: alerts {key} must be a list of event names, for example [all]")
    return ["*" if name == ALL else name for name in names]


def excludes(alert: dict, name: str) -> bool:
    return any(fnmatchcase(name, p) for p in alert["exclude-events"])


def receives(alert: dict, name: str) -> bool:
    """True when a name in `events` other than `unhandled` matches, and no exclude does."""
    return (any(fnmatchcase(name, p) for p in alert["events"] if p != UNHANDLED)
            and not excludes(alert, name))


def alerts_for(app: object) -> list[dict]:
    """The app's alerts, or the project's when no app config loads."""
    if app is not None:
        try:
            return config.merged_app(str(app))["alerts"]
        except config.ConfigError:
            pass
    try:
        root_cfg = config.load_yaml(config.find_project() / config.PROJECT_FILE)
        return parse_alerts(root_cfg.get("alerts", []), config.PROJECT_FILE)
    except config.ConfigError:
        return parse_alerts([], config.PROJECT_FILE)


FLUSH_SECONDS = 10.0
# One queue and thread per entry, so each entry gets its events in order. Each item is
# [event, entry, unhandled, state]; state goes from "open" to "done" or "fallen back".
_queues: dict[str, queue.Queue] = {}
_open: list[list] = []
_lock = threading.Condition()


def _deliver_each(items: queue.Queue) -> None:
    while True:
        item = items.get()
        event, entry, unhandled, _state = item
        with _lock:
            if item[3] != "open":
                continue
        try:
            TRANSPORTS[entry["type"]].deliver(event, entry, unhandled)
            failed = False
        except Exception as e:
            console.event_failed(f"The {console.value(entry['type'])} alert could not send "
                                 f"{console.value(event.name)}: {console.escape(str(e))}")
            failed = True
        with _lock:
            if failed and item[3] == "open":
                _fall_back(item)
            item[3] = "done" if item[3] == "open" else item[3]
            _lock.notify_all()


def _fall_back(item: list) -> None:
    event, entry, _unhandled, _state = item
    item[3] = "fallen back"
    console.event(event_text(event), f"not delivered to {entry['type']}:")


def flush() -> None:
    """Wait at most FLUSH_SECONDS for every open delivery, then print each one still open
    on the console. A Ctrl+C ends the wait early."""
    deadline = time.monotonic() + FLUSH_SECONDS
    with _lock:
        try:
            _lock.wait_for(lambda: all(item[3] != "open" for item in _open),
                           max(0.0, deadline - time.monotonic()))
        except KeyboardInterrupt:
            pass
        for item in _open:
            if item[3] == "open":
                _fall_back(item)
        _open.clear()


def emit(name: str, **params: object) -> Event:
    if name in (ALL, UNHANDLED):
        raise ValueError(f"{name!r} is a reserved word in alerts, not an event name")
    event = Event(name, datetime.now(timezone.utc), params)
    alerts = alerts_for(params.get("app"))
    unhandled = not any(receives(alert, name) for alert in alerts)
    for alert in alerts:
        if receives(alert, name):
            through_unhandled = False
        elif (unhandled and UNHANDLED in alert["events"]
              and UNHANDLED not in alert["exclude-events"] and not excludes(alert, name)):
            through_unhandled = True
        else:
            continue
        if alert["type"] == "console":
            TRANSPORTS["console"].deliver(event, alert, through_unhandled)
            continue
        item = [event, alert, through_unhandled, "open"]
        with _lock:
            _open.append(item)
            key = repr(sorted(alert.items()))
            if key not in _queues:
                _queues[key] = queue.Queue()
                threading.Thread(target=_deliver_each, args=(_queues[key],), daemon=True).start()
            _queues[key].put(item)
    return event


def app_params(app: str, provider: str) -> dict[str, object]:
    return {"project": config.find_project().name, "app": app, "provider": provider}


def run_job(app: str, provider: str, command: list[str], cwd: Path,
            env: dict[str, str] | None = None, deployed: bool = False) -> int:
    """Run `command` as one run of the job. `deployed` is True for the deployed job (a cloud
    image, the Windows task), whose trigger is `manual` when PDT_TRIGGER says so, else
    `schedule`; a local `pdt run` is always `manual`."""
    trigger = "manual" if not deployed or os.environ.get(TRIGGER_ENV) == "manual" else "schedule"
    params = {**app_params(app, provider), "run_id": storage.RUN_ID, "deployed": deployed,
              "trigger": trigger}
    emit(JOB_RUN_STARTED, **params)
    stopped: list[int] = []
    child: subprocess.Popen | None = None

    def stop(signum: int, _frame: object) -> None:
        stopped.append(signum)
        # A Ctrl+C in a Windows console reaches the command too, and Windows has no SIGTERM.
        if child is not None and os.name != "nt":
            child.send_signal(signum)

    # Before Popen, so the command does not inherit a SIGINT that a cloud image's sh ignores.
    previous = {signum: signal.signal(signum, stop) for signum in (signal.SIGINT, signal.SIGTERM)}
    try:
        child = subprocess.Popen(command, cwd=cwd, env={**(env or os.environ),
                                                        "PDT_RUN_ID": storage.RUN_ID})
        code = child.wait()
        if code < 0:
            code = 128 - code
        state = f"runs/*-{storage.RUN_ID}/"
        if stopped:
            emit(JOB_RUN_CANCELLED, **params, exit_code=code,
                 signal=signal.Signals(stopped[0]).name, state=state)
        else:
            emit(JOB_RUN_COMPLETED if code == 0 else JOB_RUN_FAILED, **params, exit_code=code,
                 state=state)
        flush()
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
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
    return run_job(app, provider, argv[1:], Path.cwd(), deployed=True)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
