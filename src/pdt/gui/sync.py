"""Fill the database from pdt commands, only as far as a page needs.

An app's runs are fetched on the first page that shows them, and again
when they are older than STALE or the user presses Refresh. A run's log
lines and artifacts are fetched the first time its page opens. Nothing
is fetched for a page nobody opened. A row keeps every run the provider
ever listed, so the history outlives the provider's retention.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from pdt import config, runs_cli
from pdt.gui.models import App, Artifact, LogLine, Project, Run
from pdt.gui.pdt_cmd import last_json, run_pdt

STALE = timedelta(minutes=5)
FIRST_COUNT = 50
# A later fetch starts this long before the newest run pdt already knows,
# so a run that was still running last time gets its final status.
OVERLAP = timedelta(hours=1)
ARTIFACT_DEPTH = 5
DONE_MARKER = "_done"


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


def is_stale(moment: datetime | None) -> bool:
    return moment is None or now() - moment >= STALE


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


def sync_runs(app: App, force: bool = False) -> bool:
    """Fetch the app's runs when they are stale or `force`. True when pdt was asked."""
    if not force and not is_stale(app.synced_at):
        return False
    code, output = run_pdt("runs", app.name, "--json", *runs_window(app))
    found = runs_cli.parse_runs(output) if code == 0 else None
    # A failed fetch is also remembered for STALE, so a broken login does
    # not start a provider script on every page load.
    app.synced_at = now()
    if found is None:
        app.sync_error = output.strip()
        app.save()
        return True
    for run in found:
        Run.objects.update_or_create(app=app, run_id=run.id, defaults={
            "started": run.started, "ended": run.ended, "status": run.status,
            "exit_code": run.exit_code})
    app.sync_error = ""
    app.save()
    return True


def sync_logs(run: Run, force: bool = False) -> bool:
    """Fetch a run's log lines once; again while it is running, or on `force`."""
    if not force and run.logs_synced_at is not None and run.status != "running":
        return False
    code, output = run_pdt("logs", run.app.name, "--id", run.run_id, "--full", "--json")
    lines = last_json(output)
    # A failed fetch keeps its error on the page and is tried again on the
    # next visit, because Log Analytics lag and an expired login both pass.
    if not isinstance(lines, list):
        run.logs_error = output.strip()
        run.save()
        return True
    run.logs_synced_at = now()
    run.lines.all().delete()
    LogLine.objects.bulk_create([
        LogLine(run=run, seq=seq, level=line.get("level") or "", message=line["message"],
                time=datetime.fromisoformat(line["time"]) if line.get("time") else None)
        for seq, line in enumerate(lines)])
    run.logs_error = ""
    run.save()
    return True


def sync_artifacts(run: Run, force: bool = False) -> bool:
    """List the files under the run's storage folder(s) once, or on `force`."""
    if not force and run.artifacts_synced_at is not None:
        return False
    app = run.app.name
    code, output = run_pdt("storage", app, "ls", "runs/", "--json")
    entries = last_json(output)
    if not isinstance(entries, list):
        run.artifacts_error = output.strip()
        run.save()
        return True
    run.artifacts_synced_at = now()
    queue = [(entry["name"].rstrip("/"), 0) for entry in entries
             if entry["name"].rstrip("/").endswith("-" + run.key)]
    files = []
    while queue:
        folder, depth = queue.pop(0)
        _code, output = run_pdt("storage", app, "ls", folder + "/", "--json")
        for entry in last_json(output) or []:
            name = entry["name"].rstrip("/")
            if entry.get("type") == "directory":
                if depth < ARTIFACT_DEPTH:
                    queue.append((name, depth + 1))
            elif name.rsplit("/", 1)[-1] != DONE_MARKER:
                files.append((name, entry.get("size")))
    run.artifacts.all().delete()
    Artifact.objects.bulk_create([Artifact(run=run, path=path, size=size) for path, size in files])
    run.artifacts_error = ""
    run.save()
    return True
