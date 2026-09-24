"""The pages of `pdt gui`.

A view reads config from the project's files, asks `sync` for the runs,
lines, or files the page lacks, and renders. A button that changes
something runs the pdt command a user would type, through `pdt_cmd`, and
shows its output as a message.
"""

from __future__ import annotations

import tempfile
from datetime import datetime
from pathlib import Path

from django.contrib import messages
from django.db.models import Avg, Count, Max, Q
from django.http import FileResponse, Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from pdt import config, runs_cli
from pdt.gui import health, sync
from pdt.gui.models import Run, Timing
from pdt.gui.pdt_cmd import run_pdt

# A cookie holds the messages, and a cookie holds about 4 KB.
MESSAGE_CHARS = 1500
APP_ACTIONS = {
    "pause": ("pause",),
    "unpause": ("unpause",),
    "start": ("run", "{name}", "--deployed"),
}


def app_config(name: str) -> dict:
    try:
        app = config.merged_app(name)
        schedule_text = (config.describe_schedule(app["schedule"])
                         if app["schedule"] is not None else "")
    except config.ConfigError as exc:
        return {"error": str(exc), "pause": False, "provider": "-", "schedule": "-",
                "schedule_text": "", "timezone": "-", "deployed": config.is_deployed(name)}
    return {"error": "", "pause": app["pause"], "provider": app["platform"].get("provider", "-"),
            "schedule": app["schedule"] or "-", "schedule_text": schedule_text,
            "timezone": app["timezone"], "deployed": config.is_deployed(name)}


def health_row(app) -> dict:
    """The `pdt health` row for an app, from the runs the database holds."""
    found = [runs_cli.Run(run.run_id, run.started, run.ended, run.status, run.exit_code)
             for run in app.runs.all()[:runs_cli.DEFAULT_RUNS]]
    if not found and app.sync_error:
        return runs_cli.health_row(app.name, None)
    return runs_cli.health_row(app.name, found)


def numbered_runs(app) -> list[dict]:
    """Every run pdt has listed, newest first, numbered like `pdt runs` numbers them."""
    return [{"run": run, "number": number, "duration": runs_cli.duration_text(
                runs_cli.Run(run.run_id, run.started, run.ended, run.status))}
            for number, run in enumerate(app.runs.all(), 1)]


def grid_params(request) -> dict:
    end = None
    raw = request.GET.get("end")
    if raw:
        try:
            end = datetime.fromisoformat(raw)
        except ValueError:
            end = None
        if end is not None and end.tzinfo is None:
            end = end.replace(tzinfo=health.DISPLAY_TZ)
    try:
        page = max(-1, min(1, int(request.GET.get("page", "0"))))
    except ValueError:
        page = 0
    return {"gran": request.GET.get("gran", health.DEFAULT_GRANULARITY), "end": end,
            "page": page, "app_name": request.GET.get("app") or None}


def index(request):
    project = sync.project_row()
    rows = []
    for app in sync.app_rows(project):
        sync.sync_runs(app)
        rows.append({"app": app, "config": app_config(app.name), "health": health_row(app)})
    return render(request, "gui/apps.html", {
        "project": project, "project_name": Path(project.path).name, "rows": rows,
        "grid": health.build_grid(project, **grid_params(request)),
    })


def app_or_404(project, name):
    app = sync.app_row(project, name)
    if app is None:
        raise Http404(f"no app named {name}")
    return app


def app_detail(request, name):
    project = sync.project_row()
    app = app_or_404(project, name)
    sync.sync_runs(app)
    params = grid_params(request)
    params["app_name"] = name
    return render(request, "gui/app.html", {
        "project": project, "project_name": Path(project.path).name, "app": app,
        "config": app_config(name), "health": health_row(app), "runs": numbered_runs(app),
        "grid": health.build_grid(project, **params),
    })


def say(request, code: int, output: str, done: str) -> None:
    text = output.strip()[-MESSAGE_CHARS:]
    if code == 0:
        messages.success(request, text or done)
    else:
        messages.error(request, text or f"pdt exited with {code}")


@require_POST
def app_action(request, name, action):
    project = sync.project_row()
    app = app_or_404(project, name)
    if action == "refresh":
        sync.sync_runs(app, force=True)
        if app.sync_error:
            messages.error(request, app.sync_error[-MESSAGE_CHARS:])
    elif action in APP_ACTIONS:
        args = [part.format(name=name) for part in APP_ACTIONS[action]]
        if action != "start":
            args.append(name)
        code, output = run_pdt(*args)
        say(request, code, output, f"pdt {' '.join(args)} done")
    else:
        raise Http404(f"no action {action}")
    return redirect(request.POST.get("next") or "app_detail", name=name)


def run_or_404(project, name, pk):
    return get_object_or_404(Run, pk=pk, app__name=name, app__project=project)


def run_detail(request, name, pk):
    project = sync.project_row()
    run = run_or_404(project, name, pk)
    sync.sync_logs(run)
    sync.sync_artifacts(run)
    lines = run.lines.all()
    errors_only = request.GET.get("errors") == "1"
    if errors_only:
        lines = lines.exclude(level__in=["DEBUG", "INFO"])
    number = next((item["number"] for item in numbered_runs(run.app) if item["run"].pk == pk), 0)
    return render(request, "gui/run.html", {
        "project": project, "project_name": Path(project.path).name, "app": run.app,
        "run": run, "number": number, "errors_only": errors_only,
        "duration": runs_cli.duration_text(
            runs_cli.Run(run.run_id, run.started, run.ended, run.status)),
        "lines": lines, "total_lines": run.lines.count(), "artifacts": run.artifacts.all(),
    })


def artifact(request, name, pk):
    project = sync.project_row()
    run = run_or_404(project, name, pk)
    path = request.GET.get("path", "")
    if not run.artifacts.filter(path=path).exists():
        raise Http404(f"no artifact {path}")
    with tempfile.TemporaryDirectory(prefix="pdt-gui-") as tmp:
        target = Path(tmp) / Path(path).name
        code, output = run_pdt("storage", name, "get", path, str(target))
        if code != 0 or not target.is_file():
            return HttpResponse(output, status=502, content_type="text/plain")
        return FileResponse(open(target, "rb"), as_attachment=True, filename=target.name)


def health_grid(request):
    project = sync.project_row()
    return render(request, "gui/_health_grid.html", {
        "grid": health.build_grid(project, **grid_params(request))})


def health_cell(request):
    project = sync.project_row()
    params = grid_params(request)
    start = None
    raw = request.GET.get("start")
    if raw:
        try:
            start = datetime.fromisoformat(raw)
        except ValueError:
            start = None
    try:
        col = int(request.GET.get("col", ""))
        row = int(request.GET.get("row", ""))
    except ValueError:
        return JsonResponse({"error": "bad col or row"}, status=400)
    if start is None or params["gran"] not in health.GRANULARITIES:
        return JsonResponse({"error": "bad start or gran"}, status=400)
    return JsonResponse({"runs": health.cell_runs(project, params["gran"], start, col, row,
                                                  params["app_name"])})


def stats(request):
    """How long each page and each pdt command took, so the slow parts are measured."""
    project = sync.project_row()

    def by_name(kind):
        return (Timing.objects.filter(kind=kind).values("name")
                .annotate(count=Count("id"), average=Avg("ms"), worst=Max("ms"),
                          failed=Count("id", filter=Q(ok=False)))
                .order_by("-average"))

    return render(request, "gui/stats.html", {
        "project": project, "project_name": Path(project.path).name,
        "commands": by_name("pdt"), "pages": by_name("page"),
        "recent": Timing.objects.all()[:40],
    })


def stylesheet(_request):
    return FileResponse(open(Path(__file__).with_name("static") / "pdt.css", "rb"),
                        content_type="text/css")
