"""Measure every page and every pdt command, so slowness is found, not guessed."""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime

from pdt.gui.models import Timing

UNTIMED_PAGES = ("stylesheet", "stats")
WORKER_THREAD = "pdt-gui-worker"


def record(kind: str, name: str, detail: str, started: datetime, seconds: float,
           ok: bool) -> None:
    # A command the worker ran counts apart from one a page waited for.
    if kind == "pdt" and threading.current_thread().name == WORKER_THREAD:
        kind = "worker"
    Timing.objects.create(kind=kind, name=name, detail=detail[:1024], started=started,
                          ms=int(seconds * 1000), ok=ok)


def command_name(args: tuple[str, ...]) -> str:
    """The name a pdt command is grouped under: `storage ls`, `run --deployed`, `runs`."""
    if args[0] == "storage" and len(args) > 2:
        return f"storage {args[2]}"
    if args[0] == "run" and "--deployed" in args:
        return "run --deployed"
    return args[0]


class TimingMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        started = datetime.now(UTC)
        clock = time.monotonic()
        response = self.get_response(request)
        match = request.resolver_match
        name = match.view_name if match else request.path
        if name not in UNTIMED_PAGES:
            record("page", name, request.get_full_path(), started, time.monotonic() - clock,
                   response.status_code < 400)
        return response
