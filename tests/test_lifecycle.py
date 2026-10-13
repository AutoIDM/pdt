import io
import sys

import pytest
from conftest import add_app
from rich.console import Console

from pdt import config, console, lifecycle


def record(monkeypatch):
    seen = []
    monkeypatch.setattr(lifecycle, "TRANSPORTS",
                        {"console": lambda event, alert: seen.append(event)})
    return seen


@pytest.mark.parametrize(("patterns", "name", "sent"), [
    (["*"], "job:run-failed", True),
    ([], "job:run-failed", False),
    (["job:run-failed"], "job:run-failed", True),
    (["job:run-failed"], "job:run-completed", False),
    (["job:*"], "job:paused", True),
    (["job:*"], "app:deploy-failed", False),
    (["!job:run-started"], "job:run-started", False),
    (["!job:run-started"], "job:run-completed", False),
    (["*", "!job:run-started"], "job:run-completed", True),
    (["!app:*", "job:run-failed"], "job:run-failed", True),
    (["job:*", "!job:run-started"], "job:run-started", False),
    (["!job:run-started", "job:*"], "job:run-started", False),
    (["*-failed", "!app:*"], "app:deploy-failed", False),
    (["*-failed", "!app:*"], "my-own:check-failed", True),
])
def test_an_event_matches_an_include_and_no_exclude(patterns, name, sent):
    [alert] = lifecycle.parse_alerts([{"type": "console", "events": patterns}], "pdt.yml")
    assert lifecycle.matches(alert, name) is sent


EVERY_EVENT = {"type": "console", "include": ["*"], "exclude": []}


def test_alerts_default_to_console_for_every_event(project):
    add_app(project, "report")
    assert config.merged_app("report")["alerts"] == [{**EVERY_EVENT, "events": ["*"]}]


def test_an_entry_with_no_events_gets_every_event(project):
    (project / "pdt.yml").write_text("alerts:\n  - type: console\n")
    add_app(project, "report")
    assert config.merged_app("report")["alerts"] == [EVERY_EVENT]


def test_the_app_alerts_replace_the_project_list(project, monkeypatch):
    (project / "pdt.yml").write_text(
        "alerts:\n  - type: console\n  - type: console\n    events: ['job:*']\n")
    add_app(project, "report", "alerts:\n  - type: console\n    events: ['*', '!job:run-started']\n")
    add_app(project, "other")
    seen = record(monkeypatch)
    lifecycle.emit("job:run-started", app="report")
    lifecycle.emit("job:run-completed", app="report")
    lifecycle.emit("job:run-started", app="other")
    lifecycle.emit("my-own:event")
    assert [(e.name, e.params.get("app")) for e in seen] == [
        ("job:run-completed", "report"),
        ("job:run-started", "other"), ("job:run-started", "other"),
        ("my-own:event", None)]


def test_validate_names_an_unknown_alerts_type_and_the_known_types(project):
    add_app(project, "report", "alerts:\n  - type: slack-webhook\n    channel: pdt\n")
    assert ("report/config.yml: alerts type 'slack-webhook' is not known; use one of: console"
            in config.validate())


def test_emit_gives_the_event_to_every_matching_alert(project, monkeypatch):
    (project / "pdt.yml").write_text("alerts:\n  - type: console\n  - type: console\n")
    seen = record(monkeypatch)
    event = lifecycle.emit("job:paused", app="report", deployed=True)
    assert seen == [event, event]
    assert event.name == "job:paused"
    assert event.params == {"app": "report", "deployed": True}


def test_the_console_handler_prints_one_bold_magenta_event_line(monkeypatch):
    out = io.StringIO()
    monkeypatch.setattr(console, "_console", Console(
        file=out, highlight=False, soft_wrap=True, force_terminal=True, color_system="standard"))
    lifecycle.emit("job:run-completed", app="report", exit_code=0)
    line = out.getvalue()
    assert line.startswith("\x1b[1;35m[event] job:run-completed at=")
    assert line.endswith(" app=report exit_code=0\x1b[0m\n")


def test_run_job_names_the_run_and_its_exit_code(project, monkeypatch):
    seen = record(monkeypatch)
    code = lifecycle.run_job("report", "local", [
        sys.executable, "-c",
        f"import os, sys; sys.exit(3 if os.environ['PDT_RUN_ID'] == {lifecycle.storage.RUN_ID!r} "
        "else 9)"], project)
    assert code == 3
    assert [event.name for event in seen] == ["job:run-started", "job:run-failed"]
    run_id = seen[0].params["run_id"]
    assert seen[1].params["exit_code"] == 3
    assert seen[1].params["run_id"] == run_id
    assert seen[1].params["state"] == f"runs/*-{run_id}/"
