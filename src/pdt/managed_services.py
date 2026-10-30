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

`lifecycle.run_job` sends the same things for a scheduled run on every
provider and for a `pdt run` on this computer:

- send_run_history: the job:run-started event, then job:run-completed or
  job:run-failed with exit_code and duration_seconds.
- send_logs: the run's output as one upload, <run_id>.log. run_job writes the
  command's output to a file as it prints it, because a cloud job's output goes
  from the container to the cloud's log service, and the run cannot read it
  back from there.
- send_artifacts: the run's storage output folder (OUTPUT_FOLDER in the app
  folder, the folder pdt.utils.storage.sync and pdt.run_powershell upload),
  zipped as one upload, <run_id>.zip. Over MAX_UPLOAD_BYTES it is skipped with
  a warning.

`deploy.provider_step` sends app:deployed after each deploy that send_run_history
covers, with the schedule, its time zone, the provider, the place, and the
repository: the project's git remote `origin` as `<host>/<path>`, with no
credentials, or null.

A network error or an HTTP 5xx is a warning, and the run goes on. A 401 means
AutoIDM refused the registration: the sender sends nothing more, the run's
work still completes, and run_job ends the run with UNAUTHORIZED_EXIT and a
message that names the registration (`refusal`).

This module is in deploy_common.LIFECYCLE_MODULES, so it imports only what
lifecycle.py's script header installs.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from pdt import config, console

KEY = "managed_services"
SEND_DEFAULTS = {"send_run_history": True, "send_logs": False, "send_artifacts": False}
CUSTOMER = re.compile(r"cus_[0-9a-f]{10}")
# The exit code of a run whose work succeeded while AutoIDM refused its registration.
# pdt uses it for nothing else; 77 is EX_NOPERM in sysexits.h.
UNAUTHORIZED_EXIT = 77
MAX_UPLOAD_BYTES = 100 * 1024 * 1024
WARNED_ENV = "PDT_MANAGED_SERVICES_WARNED"
APP_DEPLOYED = "app:deployed"


def output_folder(app_dir: Path, run_id: str) -> Path:
    return app_dir / ".pdt" / "runs" / run_id / "output"


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


def place(platform: dict) -> str | None:
    """`<provider>:<account, subscription, or project>:<region>` of a cloud provider, or None
    for the windows provider."""
    provider = platform.get("provider")
    if provider not in config.CONTAINER_PROVIDERS:
        return None
    scope = next((str(platform[key]) for key in config.FOUND_KEYS if platform.get(key)), "")
    return f"{provider}:{scope}:{platform.get('region') or ''}"


def repository(folder: Path) -> str | None:
    """The `origin` remote of the git repository at `folder` as `<host>/<path>`, with no
    scheme, user, credentials, port, or `.git`; None when there is no repository, no git, no
    `origin`, or no host in its URL."""
    try:
        found = subprocess.run(["git", "-C", str(folder), "remote", "get-url", "origin"],
                               stdin=subprocess.DEVNULL, capture_output=True, text=True)
        url = found.stdout.strip()
        if found.returncode != 0:
            return None
        if "://" in url:
            parts = urlsplit(url)
            host, path = parts.hostname, parts.path
        else:
            scp = re.fullmatch(r"(?:[^@/]+@)?([^:/]+):(.+)", url)
            host, path = (scp.group(1), scp.group(2)) if scp else (None, "")
    except (OSError, ValueError):
        return None
    path = path.strip("/").removesuffix(".git").strip("/")
    return f"{host}/{path}" if host and path else None


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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
        body = {"version": 1, "event": {"name": name, "at": at, "params": params}}
        self.send(f"the {console.value(name)} event",
                  lambda: autoidm.post(self.registration, "/v1/events", body))

    def upload(self, kind: str, data: bytes, filename: str, params: dict) -> None:
        from pdt.utils import autoidm
        body = {"kind": kind, "project": params["project"], "app": params["app"],
                "run_id": params["run_id"], "filename": filename}
        self.send(f"the {kind} {console.value(filename)}",
                  lambda: autoidm.upload(self.registration, body, data))

    def run_started(self, at: datetime, name: str, params: dict) -> None:
        if self.wants("send_run_history"):
            self.event(name, at.strftime("%Y-%m-%dT%H:%M:%SZ"), params)

    def run_ended(self, name: str, params: dict, duration: float, log: Path | None,
                  output: Path) -> bool:
        """Send the end event, the log, and the artifacts of a run; True when AutoIDM refused
        the registration."""
        if self.wants("send_run_history"):
            self.event(name, now(), {**params, "duration_seconds": round(duration, 1)})
        run_id = str(params["run_id"])
        if log is not None and log.is_file() and log.stat().st_size > 0:
            self.upload("log", log.read_bytes(), f"{run_id}.log", params)
        files = sorted(path for path in output.rglob("*") if path.is_file())
        if self.wants("send_artifacts") and files and not self.refused:
            with tempfile.TemporaryDirectory(prefix="pdt-artifacts-") as folder:
                archive = Path(folder) / f"{run_id}.zip"
                with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zipped:
                    for path in files:
                        zipped.write(path, path.relative_to(output).as_posix())
                size = archive.stat().st_size
                if size > MAX_UPLOAD_BYTES:
                    console.warn(f"AutoIDM did not get the artifacts "
                                 f"{console.value(archive.name)}: the zip is "
                                 f"{console.size(size)}, over the "
                                 f"{console.size(MAX_UPLOAD_BYTES)} limit.")
                else:
                    self.upload("artifact", archive.read_bytes(), archive.name, params)
        return self.refused

    def deployed(self, params: dict, app: dict, platform: dict) -> None:
        """Send app:deployed with the schedule, the place the app now runs, and the project's
        repository."""
        from pdt import regions
        if not self.wants("send_run_history"):
            return
        zone = regions.local_timezone() if app["timezone"] == "local" else app["timezone"]
        schedule = config.cron_expression(app["schedule"]) if app["schedule"] else None
        self.event(APP_DEPLOYED, now(), {**params, "schedule": schedule, "timezone": zone,
                                         "place": place(platform),
                                         "repository": repository(config.find_project())})

    def refusal(self) -> str:
        """The message after a 401, naming the registration AutoIDM refused."""
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


def sender(app: str) -> Sender | None:
    """The sender for `app`, or None when pdt sends nothing: no `managed_services`, the env
    var CI is set, or no registration (with a warning)."""
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
