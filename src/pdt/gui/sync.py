"""Fill the database from pdt commands.

A page fetches only what is missing: an app's runs the first time the app
is seen, a run's log lines and files the first time its page opens, and
whatever the user asks for with Refresh. `worker` keeps everything fresh
in the background, so a page almost never waits for pdt. A row keeps
every run the provider ever listed, so the history outlives the
provider's retention.
"""

from __future__ import annotations

import threading
from collections import defaultdict
from datetime import UTC, datetime, timedelta

from pdt import config, runs_cli
from pdt.gui.models import App, Artifact, LogLine, Project, Run
from pdt.gui.pdt_cmd import last_json, run_pdt

FIRST_COUNT = 50
# Every run the provider still keeps: a date this early means all of them.
ALL_RUNS_SINCE = "2000-01-01"
# A later fetch starts this long before the newest run pdt already knows,
# so a run that was still running last time gets its final status.
OVERLAP = timedelta(hours=1)
DONE_MARKER = "_done"
FOLDER_STAMP = "%Y%m%dT%H%M%SZ"
# A job names its runs/ folder at the moment it pushes, which is inside the
# run; the slack covers clock skew between the job and the provider.
FOLDER_SLACK = timedelta(minutes=1)
# A run with no end time and no later run claims folders this long at most.
LONGEST_RUN = timedelta(hours=24)

# Two page loads at once ask the provider once, not twice.
LOCKS = defaultdict(threading.Lock)


def now() -> datetime:
    return datetime.now(UTC)


def project_row() -> Project:
    row, _created = Project.objects.get_or_create(path=str(config.find_project()))
    return row


def app_rows(project: Project) -> list[App]:
    """One row per enabled app, in the order `pdt list` shows them."""
    return [App.objects.get_or_create(project=project, name=name)[0]
            for name in config.find_apps()]


def app_row(project: Project, name: str) -> App | None:
    if name not in config.find_apps():
        return None
    return App.objects.get_or_create(project=project, name=name)[0]


def is_stale(moment: datetime | None, limit: timedelta) -> bool:
    return moment is None or now() - moment >= limit


def runs_window(app: App) -> list[str]:
    """The `pdt runs` flags for the next fetch: the newest FIRST_COUNT the first
    time, then every run since the newest one known (or the oldest one still
    running), less OVERLAP."""
    newest = app.runs.order_by("-started").first()
    if newest is None:
        return ["--count", str(FIRST_COUNT)]
    since = newest.started
    oldest_running = app.runs.filter(status="running").order_by("started").first()
    if oldest_running is not None:
        since = min(since, oldest_running.started)
    return ["--since", f"{(since - OVERLAP).astimezone():%Y-%m-%dT%H:%M}"]


def sync_runs(app: App, force: bool = False, window: list[str] | None = None) -> bool:
    """Fetch the app's runs the first time, or on `force`. True when pdt was asked."""
    with LOCKS[app.name]:
        app.refresh_from_db()
        if not force and app.synced_at is not None:
            return False
        code, output = run_pdt("runs", app.name, "--json", *(window or runs_window(app)))
        found = runs_cli.parse_runs(output) if code == 0 else None
        # A failed fetch is remembered too, so a broken login does not start a
        # provider script on every page load; Refresh and the worker try again.
        app.synced_at = now()
        if found is None:
            app.sync_error = output.strip()
            app.save()
            return True
        was_running = set(app.runs.filter(status="running").values_list("run_id", flat=True))
        for run in found:
            row, _created = Run.objects.update_or_create(app=app, run_id=run.id, defaults={
                "started": run.started, "ended": run.ended, "status": run.status,
                "exit_code": run.exit_code})
            # A run read while it was going is read once more when it has ended,
            # because its last lines and its files arrive at the end.
            if run.id in was_running and run.status != "running":
                row.logs_synced_at = None
                row.artifacts_synced_at = None
                row.save()
        app.sync_error = ""
        app.save()
        return True


def sync_history(app: App) -> bool:
    """Fetch every run the provider still keeps, once per app."""
    if app.history_synced_at is not None:
        return False
    sync_runs(app, force=True, window=["--since", ALL_RUNS_SINCE])
    if app.sync_error == "":
        app.history_synced_at = now()
        app.save()
    return True


def needs_logs(run: Run) -> bool:
    return run.logs_synced_at is None or run.status == "running"


def sync_logs(runs: list[Run], force: bool = False) -> bool:
    """Fetch the log lines of several runs of one app in one `pdt logs`.

    A run's lines are fetched once, again while it is running, or on `force`.
    A failed fetch keeps its error on the page and is tried again on the
    next visit, because Log Analytics lag and an expired login both pass.
    """
    runs = [run for run in runs if force or needs_logs(run)]
    if not runs:
        return False
    ids = [part for run in runs for part in ("--id", run.run_id)]
    code, output = run_pdt("logs", runs[0].app.name, *ids, "--full", "--json")
    answer = last_json(output)
    if len(runs) == 1 and isinstance(answer, list):
        answer = {runs[0].run_id: answer}
    for run in runs:
        lines = answer.get(run.run_id) if isinstance(answer, dict) else None
        if not isinstance(lines, list):
            run.logs_error = output.strip()
            run.save()
            continue
        run.lines.all().delete()
        LogLine.objects.bulk_create([
            LogLine(run=run, seq=seq, level=line.get("level") or "", message=line["message"],
                    time=datetime.fromisoformat(line["time"]) if line.get("time") else None)
            for seq, line in enumerate(lines)])
        run.logs_error = ""
        run.logs_synced_at = now()
        run.save()
    return True


def run_folder_names(app_name: str) -> tuple[list[str] | None, str]:
    """The runs/ folders in the app's storage, or None and pdt's message."""
    code, output = run_pdt("storage", app_name, "ls", "runs/", "--json")
    entries = last_json(output)
    if not isinstance(entries, list):
        return None, output.strip()
    return [entry["name"].rstrip("/") for entry in entries], ""


def run_folders(run: Run, names: list[str],
                next_started: datetime | None = None) -> tuple[list[str], bool]:
    """The runs/ folders that belong to a run, and whether they were matched by time.

    A job whose pdt sets RUN_ID from the cloud ends the folder name with the
    run's id. An older job names it at random, so its folders are the ones written after
    the run started and before the next run did. A run the provider lists
    without an end (Azure omits it for a failed execution) must not claim
    every folder after it, so the next run's start, or LONGEST_RUN, bounds it.
    """
    exact = [name for name in names if name.endswith("-" + run.key)]
    if exact:
        return exact, False
    begin = run.started - FOLDER_SLACK
    if next_started is not None:
        finish = next_started - FOLDER_SLACK
    elif run.ended is not None:
        finish = run.ended + FOLDER_SLACK
    else:
        finish = min(now(), run.started + LONGEST_RUN)
    matched = []
    for name in names:
        stamp = name.rsplit("/", 1)[-1].split("-", 1)[0]
        try:
            written = datetime.strptime(stamp, FOLDER_STAMP).replace(tzinfo=UTC)
        except ValueError:
            continue
        if begin <= written <= finish:
            matched.append(name)
    return matched, bool(matched)


def needs_artifacts(run: Run) -> bool:
    return run.artifacts_synced_at is None or run.status == "running"


def sync_artifacts(run: Run, force: bool = False,
                   listing: tuple[list[str] | None, str] | None = None) -> bool:
    """List the files under the run's storage folder(s) once; again while it is running.

    `listing` is `run_folder_names` of the app when the caller already has it.
    """
    if not force and not needs_artifacts(run):
        return False
    app = run.app.name
    names, error = listing if listing is not None else run_folder_names(app)
    if names is None:
        run.artifacts_error = error
        run.save()
        return True
    run.artifacts_synced_at = now()
    next_started = (run.app.runs.filter(started__gt=run.started).order_by("started")
                    .values_list("started", flat=True).first())
    folders, run.artifacts_by_time = run_folders(run, names, next_started)
    files = []
    for folder in folders:
        _code, output = run_pdt("storage", app, "ls", folder + "/", "--recursive", "--json")
        for entry in last_json(output) or []:
            name = entry["name"].rstrip("/")
            if name.rsplit("/", 1)[-1] != DONE_MARKER:
                files.append((name, entry.get("size")))
    run.artifacts.all().delete()
    Artifact.objects.bulk_create([Artifact(run=run, path=path, size=size) for path, size in files])
    run.artifacts_error = ""
    run.save()
    return True
