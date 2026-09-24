"""The background worker: fills the database ahead of the pages, a little at a time.

One thread in the server process. Every TICK it refreshes each app's runs,
fetches an app's whole history the first time it sees the app, and then
takes the next few runs that still lack a log or a file list, newest
first. It keeps taking runs while there are any, so over time every run
the provider ever listed has its log and files here, and a page rarely
has to wait for pdt.

A page that opens something the worker has not reached yet fetches it
itself, as before; the two only ever repeat a fetch, never lose one.
"""

from __future__ import annotations

import logging
import threading
from datetime import timedelta

from django.db import close_old_connections
from django.db.models import Q

from pdt.gui import sync
from pdt.gui.models import App, Run

TICK = timedelta(minutes=2)
# The pause between two batches while there is a backlog, so a page's own
# fetch gets a turn at the provider.
PAUSE = timedelta(seconds=5)
# Runs handled between two checks for fresher work.
BATCH = 3

log = logging.getLogger(__name__)


def pending_runs(project) -> list[Run]:
    """Runs still lacking a log or a file list, or still running, newest first.

    A run whose fetch failed is left to the page that opens it, which tries
    again on every visit; the worker would otherwise retry it without end.
    """
    return list(Run.objects.filter(app__project=project, logs_error="", artifacts_error="")
                .filter(Q(logs_synced_at__isnull=True) | Q(artifacts_synced_at__isnull=True)
                        | Q(status="running")).order_by("-started")[:BATCH])


def tick() -> bool:
    """One pass of work. True when a run still lacked something, so more may be waiting."""
    project = sync.project_row()
    for app in sync.app_rows(project):
        if sync.is_stale(app.synced_at, TICK):
            sync.sync_runs(app, force=True)
        sync.sync_history(app)
    backlog = False
    for run in pending_runs(project):
        if run.logs_synced_at is None or run.artifacts_synced_at is None:
            backlog = True
        sync.sync_logs(run)
        sync.sync_artifacts(run)
    return backlog


def run_forever(stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            backlog = tick()
        except Exception:  # noqa: BLE001 - the worker must outlive any one bad fetch
            log.exception("pdt gui worker")
            backlog = False
        finally:
            close_old_connections()
        stop.wait((PAUSE if backlog else TICK).total_seconds())


def start() -> threading.Event:
    stop = threading.Event()
    threading.Thread(target=run_forever, args=(stop,), name="pdt-gui-worker",
                     daemon=True).start()
    return stop
