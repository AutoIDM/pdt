"""The pdt gui pages, with every pdt command answered by a fake.

Django is set up once for this module with an in-memory database, which
is what `pdt.gui.settings` picks when PDT_PROJECT is unset.
"""

import json
import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

os.environ.pop("PDT_PROJECT", None)
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "pdt.gui.settings")
import django  # noqa: E402

django.setup()
from django.core.management import call_command  # noqa: E402
from django.test import Client  # noqa: E402
from django.test.utils import setup_test_environment  # noqa: E402

call_command("migrate", verbosity=0)
setup_test_environment()

from conftest import add_app  # noqa: E402
from pdt import config  # noqa: E402
from pdt.gui import health, sync, views, worker  # noqa: E402
from pdt.gui.models import Project, Run, Timing  # noqa: E402

T0 = datetime(2026, 9, 23, 10, 0, 12, tzinfo=UTC)
RUNS = [
    {"id": "ecs/my-report/task2", "started": T0.isoformat(),
     "ended": (T0 + timedelta(seconds=30)).isoformat(), "status": "failed",
     "exit_code": 1, "number": 1},
    {"id": "ecs/my-report/task1", "started": (T0 - timedelta(days=1)).isoformat(),
     "ended": (T0 - timedelta(days=1) + timedelta(seconds=12)).isoformat(),
     "status": "succeeded", "exit_code": 0, "number": 2},
]
LINES = [
    {"time": T0.isoformat(), "level": "INFO", "message": "starting"},
    {"time": T0.isoformat(), "level": "ERROR", "message": "boom <b>"},
    {"time": None, "level": "", "message": "Traceback (most recent call last)"},
]


@pytest.fixture
def gui(project, monkeypatch):
    add_app(project, "my-report", "schedule: daily\n")
    state = SimpleNamespace(client=Client(), calls=[], answers={}, project=project)

    def fake_run_pdt(*args, timeout=900):
        state.calls.append(args)
        for key, answer in state.answers.items():
            if args[:len(key)] == key:
                return answer
        return 0, "[]\n"

    monkeypatch.setattr(sync, "run_pdt", fake_run_pdt)
    monkeypatch.setattr(views, "run_pdt", fake_run_pdt)
    yield state
    Project.objects.all().delete()
    Timing.objects.all().delete()


def runs_answer(state, runs=RUNS):
    state.answers[("runs", "my-report", "--json")] = (0, "note line\n" + json.dumps(runs) + "\n")


def test_the_index_lists_apps_and_fetches_their_runs_once(gui):
    page = gui.client.get("/")
    assert page.status_code == 200
    html = page.content.decode()
    assert "my-report" in html
    assert "not yet run" in html
    assert gui.calls == [("runs", "my-report", "--json", "--count", "50")]
    gui.client.get("/")
    assert len(gui.calls) == 1


def test_the_index_shows_the_health_row_from_the_fetched_runs(gui):
    runs_answer(gui)
    html = gui.client.get("/").content.decode()
    assert "1 of 2 succeeded" in html
    assert 'class="status failed"' in html
    assert Run.objects.count() == 2


def test_a_failed_fetch_is_shown_and_not_repeated(gui):
    gui.answers[("runs",)] = (1, "error: AWS credentials are unavailable\n")
    html = gui.client.get("/").content.decode()
    assert "could not read runs" in html
    assert "AWS credentials are unavailable" in html
    assert "unknown" in html
    gui.client.get("/")
    assert len(gui.calls) == 1


def test_the_app_page_numbers_runs_newest_first(gui):
    runs_answer(gui)
    html = gui.client.get("/apps/my-report/").content.decode()
    assert html.index("task2") < html.index("task1")
    assert "ecs/my-report/task2" in html
    assert "pdt logs my-report N" in html
    assert gui.client.get("/apps/no-such-app/").status_code == 404


def test_refresh_fetches_again_from_the_newest_known_run(gui):
    runs_answer(gui)
    gui.client.get("/apps/my-report/")
    response = gui.client.post("/apps/my-report/do/refresh/", {"next": "/"})
    assert response.status_code == 302 and response["Location"] == "/"
    since = gui.calls[-1]
    assert since[:3] == ("runs", "my-report", "--json") and since[3] == "--since"
    assert since[4] == f"{(T0 - timedelta(hours=1)).astimezone():%Y-%m-%dT%H:%M}"


def test_the_run_page_fetches_the_log_by_id_and_hides_the_analysis(gui):
    runs_answer(gui)
    gui.client.get("/apps/my-report/")
    run = Run.objects.get(run_id="ecs/my-report/task2")
    gui.answers[("logs",)] = (1, json.dumps(LINES) + "\n")
    html = gui.client.get(f"/apps/my-report/runs/{run.pk}/").content.decode()
    assert ("logs", "my-report", "--id", "ecs/my-report/task2", "--full", "--json") in gui.calls
    assert "run 1 of my-report" in html
    assert "boom &lt;b&gt;" in html
    assert "starting" in html
    assert 'data-ai-analysis' in html and "<section class=\"card\" hidden" in html
    errors = gui.client.get(f"/apps/my-report/runs/{run.pk}/?errors=1").content.decode()
    assert "starting" not in errors and "boom" in errors
    assert gui.calls.count(("logs", "my-report", "--id", "ecs/my-report/task2", "--full", "--json")) == 1


def test_a_failed_log_fetch_shows_its_error_and_is_tried_again(gui):
    runs_answer(gui)
    gui.client.get("/apps/my-report/")
    run = Run.objects.get(run_id="ecs/my-report/task2")
    gui.answers[("logs",)] = (1, "error: AWS credentials are unavailable\n")
    html = gui.client.get(f"/apps/my-report/runs/{run.pk}/").content.decode()
    assert "AWS credentials are unavailable" in html
    gui.answers[("logs",)] = (0, json.dumps(LINES) + "\n")
    html = gui.client.get(f"/apps/my-report/runs/{run.pk}/").content.decode()
    assert "AWS credentials" not in html and "starting" in html


def test_the_run_page_lists_the_files_under_the_runs_folder(gui, monkeypatch):
    runs_answer(gui)
    gui.client.get("/apps/my-report/")
    run = Run.objects.get(run_id="ecs/my-report/task2")
    gui.answers[("storage", "my-report", "ls", "runs/")] = (0, json.dumps([
        {"name": "runs/20260923T100012Z-task2", "size": None, "type": "directory"},
        {"name": "runs/20260922T100012Z-task1", "size": None, "type": "directory"}]) + "\n")
    gui.answers[("storage", "my-report", "ls", "runs/20260923T100012Z-task2/")] = (0, json.dumps([
        {"name": "runs/20260923T100012Z-task2/_done", "size": 0, "type": "file"},
        {"name": "runs/20260923T100012Z-task2/report.csv", "size": 2048, "type": "file"},
        {"name": "runs/20260923T100012Z-task2/more", "size": None, "type": "directory"}]) + "\n")
    gui.answers[("storage", "my-report", "ls", "runs/20260923T100012Z-task2/more/")] = (0, json.dumps([
        {"name": "runs/20260923T100012Z-task2/more/detail.txt", "size": 5, "type": "file"}]) + "\n")
    html = gui.client.get(f"/apps/my-report/runs/{run.pk}/").content.decode()
    assert "report.csv" in html and "detail.txt" in html and "_done" not in html
    assert "2.0\xa0KB" in html

    def fake_get(*args, timeout=900):
        gui.calls.append(args)
        with open(args[4], "w") as f:
            f.write("a,b\n")
        return 0, ""

    monkeypatch.setattr(views, "run_pdt", fake_get)
    download = gui.client.get(f"/apps/my-report/runs/{run.pk}/artifact/",
                              {"path": "runs/20260923T100012Z-task2/report.csv"})
    assert download.status_code == 200
    assert b"".join(download.streaming_content) == b"a,b\n"
    assert gui.client.get(f"/apps/my-report/runs/{run.pk}/artifact/",
                          {"path": "runs/elsewhere"}).status_code == 404


def test_files_of_an_older_job_are_matched_by_the_time_they_were_written(gui):
    runs_answer(gui)
    gui.client.get("/apps/my-report/")
    run = Run.objects.get(run_id="ecs/my-report/task2")
    gui.answers[("storage", "my-report", "ls", "runs/")] = (0, json.dumps([
        {"name": "runs/20260923T100020Z-4f1c9a2b", "size": None, "type": "directory"},
        {"name": "runs/20260923T100100Z-9d8e7f6a", "size": None, "type": "directory"},
        {"name": "runs/20260922T100015Z-1a2b3c4d", "size": None, "type": "directory"}]) + "\n")
    gui.answers[("storage", "my-report", "ls", "runs/20260923T100020Z-4f1c9a2b/")] = (0, json.dumps([
        {"name": "runs/20260923T100020Z-4f1c9a2b/report.csv", "size": 10, "type": "file"}]) + "\n")
    gui.answers[("storage", "my-report", "ls", "runs/20260923T100100Z-9d8e7f6a/")] = (0, json.dumps([
        {"name": "runs/20260923T100100Z-9d8e7f6a/late.csv", "size": 10, "type": "file"}]) + "\n")
    html = gui.client.get(f"/apps/my-report/runs/{run.pk}/").content.decode()
    assert "report.csv" in html and "late.csv" in html
    assert "older than 0.1.3" in html
    assert ("storage", "my-report", "ls", "runs/20260922T100015Z-1a2b3c4d/") not in gui.calls


def test_a_running_run_reads_its_log_and_files_again_on_each_visit(gui):
    running = [dict(RUNS[0], status="running", ended=None, exit_code=None)]
    runs_answer(gui, running)
    gui.client.get("/apps/my-report/")
    run = Run.objects.get(run_id="ecs/my-report/task2")
    gui.client.get(f"/apps/my-report/runs/{run.pk}/")
    gui.client.get(f"/apps/my-report/runs/{run.pk}/")
    assert gui.calls.count(("logs", "my-report", "--id", "ecs/my-report/task2", "--full", "--json")) == 2
    assert gui.calls.count(("storage", "my-report", "ls", "runs/", "--json")) == 2
    html = gui.client.get(f"/apps/my-report/runs/{run.pk}/").content.decode()
    assert "Refresh" not in html


def test_every_page_view_is_timed_and_shown_on_the_stats_page(gui):
    gui.client.get("/")
    gui.client.get("/apps/my-report/")
    timed = list(Timing.objects.filter(kind="page").values_list("name", flat=True))
    assert sorted(timed) == ["app_detail", "index"]
    html = gui.client.get("/stats/").content.decode()
    assert "app_detail" in html and "index" in html
    assert Timing.objects.filter(name="stats").count() == 0


def test_the_schedule_is_explained_on_hover(gui):
    html = gui.client.get("/").content.decode()
    assert 'title="daily at 00:00 (Etc/UTC)"' in html


def test_pause_unpause_and_run_now_run_the_pdt_commands(gui):
    gui.answers[("pause",)] = (0, "done: pause: true saved in my-report/config.yml\n")
    response = gui.client.post("/apps/my-report/do/pause/", {"next": "/"}, follow=True)
    assert ("pause", "my-report") in gui.calls
    assert "pause: true saved" in response.content.decode()
    gui.client.post("/apps/my-report/do/unpause/")
    assert ("unpause", "my-report") in gui.calls
    gui.answers[("run",)] = (1, "error: my-report is not deployed\n")
    response = gui.client.post("/apps/my-report/do/start/", follow=True)
    assert ("run", "my-report", "--deployed") in gui.calls
    assert "not deployed" in response.content.decode()
    assert gui.client.post("/apps/my-report/do/explode/").status_code == 404
    assert gui.client.get("/apps/my-report/do/pause/").status_code == 405


def test_the_actions_name_the_commands_they_run(gui):
    config.mark_deployed("my-report", True)
    (gui.project / "my-report" / "config.yml").write_text("schedule: daily\npause: true\n")
    html = gui.client.get("/apps/my-report/").content.decode()
    assert 'title="pdt unpause my-report"' in html
    assert 'title="pdt run my-report --deployed"' in html
    assert ">paused<" in html


def test_the_health_grid_shows_runs_and_projects_the_schedule(gui):
    runs_answer(gui)
    gui.client.get("/")
    grid = gui.client.get("/health/", {"gran": "day"}).content.decode()
    assert "<svg" in grid and 'class="hg-bad"' in grid
    assert 'class="hg-sched"' in grid
    start = health.build_grid(sync.project_row(), "day")["data"]["start"]
    cell = gui.client.get("/health/cell.json", {"gran": "day", "start": start, "col": 0, "row": 0})
    assert cell.status_code == 200 and "runs" in cell.json()
    assert gui.client.get("/health/cell.json", {"gran": "day", "col": "x", "row": 0}).status_code == 400


def test_a_paused_app_projects_no_future_runs(gui):
    (gui.project / "my-report" / "config.yml").write_text("schedule: daily\npause: true\n")
    assert health.scheduled_apps() == []
    (gui.project / "my-report" / "config.yml").write_text("schedule: daily\n")
    assert health.scheduled_apps() == [("my-report", "0 0 * * *", "Etc/UTC")]


def test_the_stylesheet_is_served_from_the_package(gui):
    response = gui.client.get("/static/pdt.css")
    assert response.status_code == 200
    assert response["Content-Type"].startswith("text/css")
    assert b".hg-bad" in b"".join(response.streaming_content)


def test_the_worker_refreshes_runs_backfills_history_and_drains_newest_first(gui):
    runs_answer(gui)
    assert worker.tick() is True
    fetches = [call for call in gui.calls if call[:2] == ("runs", "my-report")]
    assert fetches[0] == ("runs", "my-report", "--json", "--count", "50")
    assert fetches[1] == ("runs", "my-report", "--json", "--since", "2000-01-01")
    logs = [call for call in gui.calls if call[0] == "logs"]
    assert [call[3] for call in logs] == ["ecs/my-report/task2", "ecs/my-report/task1"]
    files = [call for call in gui.calls if call[:4] == ("storage", "my-report", "ls", "runs/")]
    assert len(files) == 2
    assert Run.objects.filter(logs_synced_at__isnull=True).count() == 0
    assert worker.tick() is False
    assert len([call for call in gui.calls if call[:2] == ("runs", "my-report")]) == 2


def test_the_worker_reads_a_running_run_again_but_reports_no_backlog(gui):
    runs_answer(gui, [dict(RUNS[0], status="running", ended=None, exit_code=None)])
    assert worker.tick() is True
    assert worker.tick() is False
    assert len([call for call in gui.calls if call[0] == "logs"]) == 2


def test_the_worker_leaves_a_failed_fetch_to_the_page(gui):
    runs_answer(gui)
    gui.answers[("logs",)] = (1, "error: AWS credentials are unavailable\n")
    assert worker.tick() is True
    assert worker.tick() is False
    assert len([call for call in gui.calls if call[0] == "logs"]) == 2


def test_the_worker_counts_its_commands_apart_from_the_pages(gui):
    from pdt.gui import timing
    thread = worker.threading.current_thread()
    thread.name = timing.WORKER_THREAD
    try:
        timing.record("pdt", "runs", "runs my-report", datetime.now(UTC), 0.5, True)
    finally:
        thread.name = "MainThread"
    timing.record("pdt", "runs", "runs my-report", datetime.now(UTC), 0.5, True)
    assert sorted(Timing.objects.values_list("kind", flat=True)) == ["pdt", "worker"]
    html = gui.client.get("/stats/").content.decode()
    assert "Background worker" in html
