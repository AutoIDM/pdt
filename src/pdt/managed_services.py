"""Managed services: what a project sends to AutoIDM, and the one sender every provider uses.

A project turns it on in pdt.yml:

    managed_services:
      customer: cus_7a1c09e4b2
      send_run_history: true    # or a list of app names; true by default
      send_logs: [report]       # or true; false by default
      send_artifacts: false     # or a list, or true; false by default

With no `managed_services`, pdt sends nothing and says nothing. Otherwise
`sender` finds the registration (pdt.utils.autoidm): the cloud registration in
the environment of a cloud job, else this computer's registration for the
customer. When the env var CI is set, it finds none, so a CI job sends nothing.
A project with no registration gets one warning in each pdt command, and
sends nothing; WARNED_ENV keeps it to one warning across the processes of the
command.

pdt sends the same things for a scheduled run on every provider and for a
`pdt run` on this computer:

- send_run_history: the job:run-started event, then job:run-completed or
  job:run-failed with exit_code and duration_seconds, and app:deployed after
  each deploy, with the schedule, its time zone, the provider, and the place.
- send_logs: the run's output as one upload, <run_id>.log. run_job writes the
  command's output to a file as it prints it, because a cloud job's output goes
  from the container to the cloud's log service, and the run cannot read it
  back from there.
- send_artifacts: the run's storage output folder (`output_folder`, the folder
  pdt.utils.storage.sync and pdt.run_powershell upload), zipped as one upload,
  <run_id>.zip. A link to a place outside the folder, or into a loop, is
  skipped with a warning, and an output folder that is itself a link sends
  nothing.

A log or zip over MAX_UPLOAD_BYTES is skipped with a warning.

Every event and upload names the project and the app by `backend_name`.

`accept` is the managed row of lifecycle.SINKS. It selects job:run-started and
app:deploy-completed, which it sends as app:deployed, and returns at once. One
thread sends everything in order. `lifecycle.run_job` calls `start_run` before
the command runs and `end_run` after it exits. `end_run` puts the end event, the
log, and the artifacts after what is already in the queue, and returns when
all of it is sent or EXIT_WAIT_SECONDS end, so run_job knows of a refusal before
it emits the end event for the alerts. `accept` does not select the end events.
`wait` gives up on what is not sent when its time limit ends, with one warning
for each item.

A network error, an HTTP 5xx, a file pdt cannot read or zip, or a late send is
a warning, and the run goes on with its own exit code. A 401 means AutoIDM
refused the registration: the sender sends nothing more, the run's work still
completes, and run_job ends the run with UNAUTHORIZED_EXIT and a message that
names the registration (`refusal`).

This module is in deploy_common.LIFECYCLE_MODULES, so it imports only what
lifecycle.py's script header installs.
"""

from __future__ import annotations

import hashlib
import os
import queue
import re
import tempfile
import threading
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from pdt import config, console

KEY = "managed_services"
SEND_DEFAULTS = {"send_run_history": True, "send_logs": False, "send_artifacts": False}
CUSTOMER = re.compile(r"cus_[0-9a-f]{10}")
# The exit code of a run whose work succeeded while AutoIDM refused its registration.
# pdt uses it for nothing else; 77 is EX_NOPERM in sysexits.h.
UNAUTHORIZED_EXIT = 77
MAX_UPLOAD_BYTES = 100 * 1024 * 1024
WARNED_ENV = "PDT_MANAGED_SERVICES_WARNED"
JOB_RUN_STARTED = "job:run-started"
APP_DEPLOY_COMPLETED = "app:deploy-completed"
APP_DEPLOYED = "app:deployed"
TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
EXIT_WAIT_SECONDS = 15.0
# The project, app, run, and file names that AutoIDM takes.
BACKEND_NAME = re.compile(r"(?!\.{1,2}$)[A-Za-z0-9._-]{1,128}")


def backend_name(name: str) -> str:
    """`name` when BACKEND_NAME takes it. Else each run of other characters becomes `-`, and
    the first 8 hex digits of the SHA-256 of `name` follow, so `My Jobs` is
    `My-Jobs-<hash>` in every event and upload, and differs from `My-Jobs`."""
    if BACKEND_NAME.fullmatch(name):
        return name
    readable = re.sub(r"[^A-Za-z0-9._-]+", "-", name)[:119]
    return f"{readable}-{hashlib.sha256(name.encode()).hexdigest()[:8]}"


def output_folder(app_dir: Path, run_id: str, fallback: Path) -> Path:
    """The run's output folder: .pdt/runs/<run_id>/output in the app folder, the one
    storage.sync gives a Python app, or `fallback` when pdt cannot write in the app folder. It
    makes the folder that holds it."""
    folder = app_dir / ".pdt" / "runs" / run_id / "output"
    try:
        folder.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        fallback.parent.mkdir(parents=True, exist_ok=True)
        return fallback
    return folder


def parse(raw: object, where: str) -> dict:
    example = "for example customer: cus_7a1c09e4b2"
    if not isinstance(raw, dict):
        raise config.ConfigError(f"{where}: managed_services must hold customer, {example}")
    for key in raw:
        if key != "customer" and key not in SEND_DEFAULTS:
            raise config.ConfigError(
                f"{where}: managed_services: unknown key {key!r}; it takes: customer, "
                + ", ".join(SEND_DEFAULTS))
    customer = raw.get("customer")
    if not (isinstance(customer, str) and CUSTOMER.fullmatch(customer)):
        raise config.ConfigError(
            f"{where}: managed_services: customer must be the customer ID that AutoIDM gave "
            f"you, {example}")
    parsed = {"customer": customer}
    for key, default in SEND_DEFAULTS.items():
        value = raw.get(key, default)
        if not (isinstance(value, bool) or (
                isinstance(value, list) and all(isinstance(name, str) for name in value))):
            raise config.ConfigError(
                f"{where}: managed_services: {key} must be true, false, or a list of app names")
        parsed[key] = value
    return parsed


def problems(raw: object, apps: list[str]) -> list[str]:
    """What `pdt validate` says about `managed_services`: its shape, then each app name in a
    send_* list that is not an app of the project."""
    try:
        parsed = parse(raw, config.PROJECT_FILE)
    except config.ConfigError as e:
        return [str(e)]
    return [f"{config.PROJECT_FILE}: managed_services: {key} names {name!r}, which is not an "
            "app in this project"
            for key in SEND_DEFAULTS if isinstance(parsed[key], list)
            for name in parsed[key] if name not in apps]


def settings() -> dict | None:
    """The project's parsed `managed_services`, or None when pdt.yml has none."""
    root = config.load_yaml(config.find_project() / config.PROJECT_FILE)
    if KEY not in root:
        return None
    return parse(root[KEY], config.PROJECT_FILE)


def sends(found: dict, key: str, app: str) -> bool:
    value = found[key]
    return value is True or (isinstance(value, list) and app in value)


def warn_once(message: str) -> None:
    if os.environ.get(WARNED_ENV):
        return
    os.environ[WARNED_ENV] = "1"
    console.warn(message)


def monitored(what: str, call: Callable[[], object], default: object = None):
    """`call()`, or `default` after one warning when it raises: a failure in what pdt sends to
    AutoIDM never changes the run or its exit code."""
    try:
        return call()
    except Exception as e:
        console.warn(f"AutoIDM did not get {what}: {console.escape(f'{type(e).__name__}: {e}')}")
        return default


def place(platform: dict) -> str | None:
    """`<provider>:<account, subscription, or project>:<region>` of a cloud provider, or None
    for the windows provider."""
    provider = platform.get("provider")
    if provider not in config.CONTAINER_PROVIDERS:
        return None
    scope = next((str(platform[key]) for key in config.FOUND_KEYS if platform.get(key)), "")
    return f"{provider}:{scope}:{platform.get('region') or ''}"


@dataclass
class Sender:
    app: str
    settings: dict
    registration: object
    refused: bool = False

    def wants(self, key: str) -> bool:
        return sends(self.settings, key, self.app)

    def send(self, what: str, call) -> None:
        """Make one request; a 401 marks the registration refused, and any other failure is a
        warning. After a 401 it sends nothing."""
        from pdt.utils import autoidm
        if self.refused:
            return
        try:
            call()
        except autoidm.AutoidmError as e:
            if e.status == 401:
                self.refused = True
            else:
                console.warn(f"AutoIDM did not get {what}: {console.escape(str(e))}")

    def event(self, name: str, at: str, params: dict) -> None:
        from pdt.utils import autoidm
        params = {**params, "project": backend_name(params["project"]),
                  "app": backend_name(params["app"])}
        body = {"version": 1, "event": {"name": name, "at": at, "params": params}}
        self.send(f"the {console.value(name)} event",
                  lambda: autoidm.post(self.registration, "/v1/events", body))

    def upload(self, kind: str, data: bytes, filename: str, params: dict) -> None:
        from pdt.utils import autoidm
        body = {"kind": kind, "project": backend_name(params["project"]),
                "app": backend_name(params["app"]),
                "run_id": params["run_id"], "filename": filename}
        self.send(f"the {kind} {console.value(filename)}",
                  lambda: autoidm.upload(self.registration, body, data))

    def upload_log(self, log: Path, filename: str, params: dict) -> None:
        try:
            size = log.stat().st_size
            if size > MAX_UPLOAD_BYTES:
                console.warn(f"AutoIDM did not get the log {console.value(filename)}: it is "
                             f"{console.size(size)}, over the {console.size(MAX_UPLOAD_BYTES)} "
                             "limit.")
                return
            with log.open("rb") as kept:
                data = kept.read(size)
        except OSError as e:
            console.warn(f"AutoIDM did not get the log {console.value(filename)}: "
                         f"{console.escape(str(e))}")
            return
        if data:
            self.upload("log", data, filename, params)

    def upload_artifacts(self, output: Path, filename: str, params: dict) -> None:
        """Zip the files under `output` and upload the zip. A link that leads out of `output`
        is skipped with a warning, so a run cannot send a file from elsewhere on the computer.
        When `output` itself is a link or a Windows junction, nothing is sent."""
        if output.is_symlink() or output.is_junction():
            console.warn(f"AutoIDM did not get the artifacts {console.value(filename)}: "
                         f"{console.value(output)} links to another place.")
            return
        root = output.resolve()
        files = []
        for path in sorted(output.rglob("*")):
            try:
                inside = path.resolve().is_relative_to(root)
            except (OSError, RuntimeError):
                inside = False
            if not inside:
                console.warn(f"AutoIDM did not get {console.value(path)}: it links to a place "
                             f"outside {console.value(output)}, or into a loop.")
            elif path.is_file():
                files.append(path)
        if not files:
            return
        with tempfile.TemporaryDirectory(prefix="pdt-artifacts-") as folder:
            archive = Path(folder) / filename
            with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED,
                                 strict_timestamps=False) as zipped:
                for path in files:
                    zipped.write(path, path.relative_to(output).as_posix())
            size = archive.stat().st_size
            if size > MAX_UPLOAD_BYTES:
                console.warn(f"AutoIDM did not get the artifacts {console.value(filename)}: the "
                             f"zip is {console.size(size)}, over the "
                             f"{console.size(MAX_UPLOAD_BYTES)} limit.")
                return
            self.upload("artifact", archive.read_bytes(), filename, params)

    def refusal(self) -> str:
        from pdt.utils import autoidm
        found = self.registration
        if found.source == autoidm.ID_ENV:
            return (f"AutoIDM refused the cloud registration "
                    f"{console.value(found.installation_id)} of {console.value(self.app)} "
                    f"({console.value(autoidm.ID_ENV)} in the app's secret), so it got nothing "
                    "from this run. Ask AutoIDM whether it was revoked.")
        key = Path(found.source).with_suffix(".key")
        return (f"AutoIDM refused the registration {console.value(found.installation_id)} in "
                f"{console.value(found.source)}, so it got nothing from this run. Ask AutoIDM "
                f"whether it was revoked. To register this computer again, delete "
                f"{console.value(found.source)} and {console.value(key)}, then run "
                f"{console.value('pdt register --code CODE')} with a new code.")


def find_registration(customer: str, machine: bool = False):
    """The registration for `customer` (only this computer's when `machine`), or None with
    one warning."""
    from pdt.utils import autoidm
    try:
        found = (autoidm.machine_registration(customer) if machine
                 else autoidm.registration(customer))
    except (autoidm.AutoidmError, ValueError, KeyError) as e:
        warn_once(f"pdt cannot use the AutoIDM registration for {console.value(customer)}: "
                  f"{console.escape(str(e))}. pdt sends nothing to AutoIDM.")
        return None
    if found is None:
        warn_once(f"This computer is not registered to AutoIDM for {console.value(customer)}, "
                  f"so pdt sends nothing to AutoIDM. To register it, run "
                  f"{console.value('pdt register --code CODE')} with the code from AutoIDM.")
    return found


def in_ci() -> bool:
    return os.environ.get("CI", "").strip() != ""


# One sender for each app, so a refusal lasts for the whole process.
_senders: dict[str, Sender | None] = {}


def sender(app: str) -> Sender | None:
    """The sender for `app`, or None when pdt sends nothing: no `managed_services`, the env
    var CI is set, or no registration (with a warning)."""
    if app not in _senders:
        _senders[app] = monitored("this run", lambda: find_sender(app))
    return _senders[app]


def find_sender(app: str) -> Sender | None:
    if in_ci():
        return None
    try:
        found = settings()
    except config.ConfigError as e:
        warn_once(f"{console.escape(str(e))}. pdt sends nothing to AutoIDM.")
        return None
    if found is None:
        return None
    registration = find_registration(found["customer"])
    return None if registration is None else Sender(app, found, registration)


# Each item is [sender, what, call, state]; state goes from "open" to "done" or "late".
_queue: queue.Queue = queue.Queue()
_open: list[list] = []
_lock = threading.Condition()
_thread: threading.Thread | None = None


def _send_each() -> None:
    while True:
        item = _queue.get()
        found, what, call, _state = item
        with _lock:
            skip = item[3] != "open"
        if skip:
            continue
        try:
            if not found.refused:
                call()
        except Exception as e:
            with _lock:
                late = item[3] != "open"
            if not late:
                console.warn(f"AutoIDM did not get {what}: {console.escape(str(e))}")
        with _lock:
            if item[3] == "open":
                item[3] = "done"
            _lock.notify_all()


def queue_send(found: Sender, what: str, call: Callable[[], None]) -> None:
    global _thread
    item = [found, what, call, "open"]
    with _lock:
        _open.append(item)
        if _thread is None:
            _thread = threading.Thread(target=_send_each, daemon=True)
            _thread.start()
        _queue.put(item)


def wait(seconds: float | None = None) -> None:
    """Wait at most `seconds`, by default EXIT_WAIT_SECONDS, for everything in the queue, then
    give up on what is not sent, with one warning for each item."""
    limit = EXIT_WAIT_SECONDS if seconds is None else seconds
    with _lock:
        _lock.wait_for(lambda: all(item[3] != "open" for item in _open), limit)
        for item in _open:
            if item[3] == "open":
                item[3] = "late"
                what = item[1]
                console.warn(f"AutoIDM did not get {what}: pdt stopped waiting for it.")
        _open.clear()
    console.drain()


def accept(event) -> None:
    """Select and queue one event of lifecycle.emit."""
    if event.name not in (JOB_RUN_STARTED, APP_DEPLOY_COMPLETED):
        return
    app = str(event.params["app"])
    found = sender(app)
    if found is None or not found.wants("send_run_history"):
        return
    at = event.at.strftime(TIME_FORMAT)
    if event.name == JOB_RUN_STARTED:
        queue_send(found, f"the {console.value(event.name)} event",
                   lambda: found.event(event.name, at, event.params))
    else:
        queue_send(found, f"the {console.value(APP_DEPLOYED)} event",
                   lambda: found.event(APP_DEPLOYED, at,
                                       {**event.params, **deployed_params(app)}))


def deployed_params(app_name: str) -> dict[str, object]:
    from pdt import regions
    app = config.merged_app(app_name)
    zone = regions.local_timezone() if app["timezone"] == "local" else app["timezone"]
    schedule = config.cron_expression(app["schedule"]) if app["schedule"] else None
    return {"schedule": schedule, "timezone": zone,
            "place": place(config.deployed_platform(app_name))}


@dataclass(frozen=True)
class Run:
    sender: Sender | None = None
    scratch: Path | None = None
    log: Path | None = None
    output: Path | None = None


def start_run(app: str, run_id: str, app_dir: Path) -> Run:
    """What run_job keeps for AutoIDM: the file for the command's output and the run's output
    folder, each only when the project sends it. The caller deletes `scratch` after the run."""
    found = sender(app)
    if found is None or not (found.wants("send_logs") or found.wants("send_artifacts")):
        return Run(found)
    try:
        scratch = Path(tempfile.mkdtemp(prefix=f"pdt-{run_id}-"))
    except OSError as e:
        console.warn(f"pdt cannot keep the output or the artifacts of this run for AutoIDM: "
                     f"{console.escape(str(e))}")
        return Run(found)
    log = scratch / f"{run_id}.log" if found.wants("send_logs") else None
    output = None
    if found.wants("send_artifacts"):
        output = monitored("the artifacts of this run",
                           lambda: output_folder(app_dir, run_id, scratch / "output"))
    return Run(found, scratch, log, output)


def end_run(run: Run, name: str, params: dict, duration: float) -> bool:
    """Send the end event, the log, and the artifacts of a run, after what is in the queue, for
    at most EXIT_WAIT_SECONDS in total. True when AutoIDM refused the registration."""
    found = run.sender
    if found is None:
        return False
    run_id = str(params["run_id"])
    if found.wants("send_run_history"):
        at = datetime.now(timezone.utc).strftime(TIME_FORMAT)
        ended = {**params, "duration_seconds": round(duration, 1)}
        queue_send(found, f"the {console.value(name)} event", lambda: found.event(name, at, ended))
    if run.log is not None:
        filename = f"{run_id}.log"
        queue_send(found, f"the log {console.value(filename)}",
                   lambda: found.upload_log(run.log, filename, params))
    if run.output is not None:
        zipname = f"{run_id}.zip"

        def send_artifacts() -> None:
            if run.output.is_dir():
                found.upload_artifacts(run.output, zipname, params)

        queue_send(found, f"the artifacts {console.value(zipname)}", send_artifacts)
    wait()
    return found.refused
