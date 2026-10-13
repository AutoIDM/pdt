import io
import sys

from rich.console import Console

from pdt import console, lifecycle


def test_emit_gives_the_event_to_every_handler(monkeypatch):
    seen = []
    monkeypatch.setattr(lifecycle, "HANDLERS", [seen.append, seen.append])
    event = lifecycle.emit("job:paused", app="report", deployed=True)
    assert seen == [event, event]
    assert event.name == "job:paused"
    assert event.params == {"app": "report", "deployed": True}


def test_the_console_handler_prints_one_bold_magenta_event_line(monkeypatch):
    out = io.StringIO()
    monkeypatch.setattr(console, "_console", Console(
        file=out, highlight=False, soft_wrap=True, force_terminal=True, color_system="standard"))
    lifecycle.emit("job:completed", app="report", exit_code=0)
    line = out.getvalue()
    assert line.startswith("\x1b[1;35m[event] job:completed at=")
    assert line.endswith(" app=report exit_code=0\x1b[0m\n")


def test_run_job_names_the_run_and_its_exit_code(project, monkeypatch):
    seen = []
    monkeypatch.setattr(lifecycle, "HANDLERS", [seen.append])
    code = lifecycle.run_job("report", "local", [
        sys.executable, "-c",
        f"import os, sys; sys.exit(3 if os.environ['PDT_RUN_ID'] == {lifecycle.storage.RUN_ID!r} "
        "else 9)"], project)
    assert code == 3
    assert [event.name for event in seen] == ["job:run-started", "job:failed"]
    run_id = seen[0].params["run_id"]
    assert seen[1].params["exit_code"] == 3
    assert seen[1].params["run_id"] == run_id
    assert seen[1].params["state"] == f"runs/*-{run_id}/"
