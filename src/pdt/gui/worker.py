"""The background worker: fills the database ahead of the pages, a little at a time.

One thread in the server process. Every TICK it refreshes each app's runs,
fetches an app's whole history the first time it sees the app, and then
takes the next few runs that still lack a log or a file list, newest
first. It keeps taking runs while there are any, so over time every run
the provider ever listed has its log and files here, and a page rarely
has to wait for pdt.

A batch asks pdt once per app for all its logs and once per app for the
list of storage folders, because each pdt command costs seconds before it
does any work. A page never fetches a log or a file list itself: it asks
for the run with `ask`, the worker takes that run before the rest, and
the page refreshes itself until the run has what it lacks.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from datetime import timedelta

from django.db import close_old_connections
from django.db.models import Q

from pdt.gui import sync
from pdt.gui.models import Run

TICK = timedelta(minutes=2)
# An app with a run in progress is asked again this often, so the end of a
# run shows within a minute; Azure itself reports an execution as running
# for a while after its container stops.
RUNNING_TICK = timedelta(seconds=30)
# The pause between two batches while there is a backlog, so a page's own
# fetch gets a turn at the provider.
PAUSE = timedelta(seconds=5)
# Runs handled between two checks for fresher work.
BATCH = 5

log = logging.getLogger(__name__)

# Runs a page is waiting for, oldest request first, and the event that ends
# the worker's sleep when one arrives.
ASKED = deque()
WAKE = threading.Event()


def ask(run: Run) -> None:
    if run.pk not in ASKED:
        ASKED.append(run.pk)
    WAKE.set()


def pending_runs(project) -> list[Run]:
    """Runs still lacking a log or a file list, or still running: the ones a
    page asked for first, then newest first.

    A run whose fetch failed by itself is left alone until a page asks for
    it again; the worker would otherwise retry it without end.
    """
    asked = list(ASKED)
    ASKED.clear()
    wanted = list(Run.objects.filter(pk__in=asked, app__project=project))
    wanted.sort(key=lambda run: asked.index(run.pk))
    rest = (Run.objects.filter(app__project=project, logs_error="", artifacts_error="")
            .filter(Q(logs_synced_at__isnull=True) | Q(artifacts_synced_at__isnull=True)
                    | Q(status="running")).exclude(pk__in=asked).order_by("-started"))
    return (wanted + list(rest))[:max(BATCH, len(wanted))]


def tick() -> bool:
    """One pass of work. True when a run still lacked something, so more may be waiting."""
    project = sync.project_row()
    for app in sync.app_rows(project):
        limit = RUNNING_TICK if app.runs.filter(status="running").exists() else TICK
        if sync.is_stale(app.synced_at, limit):
            sync.sync_runs(app, force=True)
        sync.sync_history(app)
    pending = pending_runs(project)
    backlog = any(run.logs_synced_at is None or run.artifacts_synced_at is None
                  for run in pending)
    by_app = {}
    for run in pending:
        by_app.setdefault(run.app_id, []).append(run)
    for runs in by_app.values():
        sync.sync_logs(runs, force=any(run.logs_error for run in runs))
        listing = None
        for run in runs:
            if sync.needs_artifacts(run) or run.artifacts_error:
                if listing is None:
                    listing = sync.run_folder_names(run.app.name)
                sync.sync_artifacts(run, force=bool(run.artifacts_error), listing=listing)
    return backlog or bool(ASKED)


def run_forever(stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            backlog = tick()
        except Exception:  # noqa: BLE001 - the worker must outlive any one bad fetch
            log.exception("pdt gui worker")
            backlog = False
        finally:
            close_old_connections()
        WAKE.clear()
        if not backlog:
            running = Run.objects.filter(status="running").exists()
            WAKE.wait((RUNNING_TICK if running else TICK).total_seconds())
        stop.wait(PAUSE.total_seconds() if backlog else 0)


def start() -> threading.Event:
    stop = threading.Event()
    threading.Thread(target=run_forever, args=(stop,), name="pdt-gui-worker",
                     daemon=True).start()
    return stop
