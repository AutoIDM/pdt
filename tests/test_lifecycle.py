import io
import os
import signal
import sys
import threading
import time
from types import SimpleNamespace

import pytest
from conftest import add_app, started
from rich.console import Console

from pdt import cli, config, console, deploy, lifecycle, scaffold


def record(monkeypatch, **more):
    seen = []
    transports = {"console": lifecycle.Transport(
        "console", frozenset(), lambda event, entry, unhandled: seen.append((event.name, unhandled)))}
    monkeypatch.setattr(lifecycle, "TRANSPORTS", {**transports, **more})
    return seen


def colour_output(monkeypatch):
    out = io.StringIO()
    monkeypatch.setattr(console, "_console", Console(
        file=out, highlight=False, soft_wrap=True, force_terminal=True, color_system="standard"))
    return out


@pytest.mark.parametrize(("events", "exclude", "name", "sent"), [
    (["all"], [], "job:run-failed", True),
    ([], [], "job:run-failed", False),
    (["job:run-failed"], [], "job:run-completed", False),
    (["job:*"], [], "job:paused", True),
    (["job:*"], [], "app:deploy-failed", False),
    (None, ["job:run-started"], "job:run-completed", False),
    (["all"], ["job:run-started"], "job:run-started", False),
    (["all"], ["job:run-started"], "job:run-completed", True),
    (["*-failed"], ["app:*"], "app:deploy-failed", False),
    (["*-failed"], ["app:*"], "my-own:check-failed", True),
])
def test_an_entry_receives_an_event_in_events_and_not_in_exclude_events(
        events, exclude, name, sent):
    entry = {"type": "console", "exclude-events": exclude}
    if events is not None:
        entry["events"] = events
    alert = lifecycle.parse_alerts([entry], "pdt.yml")[0]
    assert lifecycle.receives(alert, name) is sent


def test_an_entry_with_neither_events_nor_exclude_events_is_an_error():
    with pytest.raises(config.ConfigError, match="needs events, for example events: \\[all\\]"):
        lifecycle.parse_alerts([{"type": "console"}], "pdt.yml")


def test_an_unknown_entry_key_names_the_key_and_the_keys_of_its_type(project, monkeypatch):
    add_app(project, "report", "alerts:\n  - type: console\n    event: [all]\n")
    assert ("report/config.yml: alerts: unknown key 'event'; a console entry takes: "
            "type, events, exclude-events" in config.validate())
    monkeypatch.setitem(lifecycle.TRANSPORTS, "webhook", lifecycle.Transport(
        "webhook", frozenset({"url"}), lambda event, entry, unhandled: None))
    assert lifecycle.parse_alerts([{"type": "webhook", "events": ["all"], "url": "x"}], "pdt.yml")


def test_validate_names_an_unknown_alerts_type_and_the_known_types(project):
    add_app(project, "report", "alerts:\n  - type: slack-webhook\n    channel: pdt\n")
    assert ("report/config.yml: alerts type 'slack-webhook' is not known; use one of: console"
            in config.validate())


@pytest.mark.parametrize(("alerts", "seen"), [
    ("", [("job:paused", True)]),
    ("alerts:\n  - type: console\n    events: [all]\n", [("job:paused", False)]),
    ("alerts:\n  - type: console\n    events: [app:*]\n", [("job:paused", True)]),
    ("alerts:\n  - type: console\n    exclude-events: [unhandled]\n", []),
    ("alerts:\n  - type: console\n    events: [unhandled]\n    exclude-events: [job:*]\n", []),
    ("alerts:\n  - type: console\n    events: [unhandled, job:*]\n", [("job:paused", False)]),
])
def test_an_event_no_entry_receives_goes_to_the_unhandled_entries(project, monkeypatch,
                                                                  alerts, seen):
    (project / "pdt.yml").write_text(alerts)
    got = record(monkeypatch)
    lifecycle.emit("job:paused")
    assert got == seen


def test_every_entry_that_receives_an_event_gets_it_once(project, monkeypatch):
    (project / "pdt.yml").write_text(
        "alerts:\n  - type: console\n    events: [all]\n  - type: console\n"
        "    events: [job:*, job:paused]\n")
    seen = record(monkeypatch)
    lifecycle.emit("job:paused")
    assert seen == [("job:paused", False), ("job:paused", False)]


def test_the_app_alerts_replace_the_project_list(project, monkeypatch):
    (project / "pdt.yml").write_text("alerts:\n  - type: console\n    events: [job:*]\n")
    add_app(project, "report", "alerts:\n  - type: console\n    exclude-events: [unhandled]\n")
    seen = record(monkeypatch)
    lifecycle.emit("job:paused", app="report")
    lifecycle.emit("job:paused")
    assert seen == [("job:paused", False)]


@pytest.mark.parametrize("name", ["all", "unhandled"])
def test_all_and_unhandled_are_not_event_names(project, name):
    with pytest.raises(ValueError, match="reserved"):
        lifecycle.emit(name)


def test_the_console_prints_a_handled_event_bold_and_an_unhandled_one_dim(project, monkeypatch):
    out = colour_output(monkeypatch)
    lifecycle.emit("job:run-completed", app="report", exit_code=0)
    (project / "pdt.yml").write_text("alerts:\n  - type: console\n    events: [all]\n")
    lifecycle.emit("job:run-completed", app="report", exit_code=0)
    unhandled, handled = out.getvalue().splitlines()
    assert unhandled.startswith("\x1b[2;35m[event] unhandled job:run-completed at=")
    assert handled.startswith("\x1b[1;35m[event] job:run-completed at=")
    assert handled.endswith(" app=report exit_code=0\x1b[0m")


def two_entries(project, kind):
    (project / "pdt.yml").write_text(
        f"alerts:\n  - type: {kind}\n    events: [all]\n  - type: console\n    events: [all]\n")


def test_a_failing_transport_warns_falls_back_to_the_console_and_the_run_goes_on(
        project, monkeypatch, capsys):
    def deliver(event, entry, unhandled):
        raise RuntimeError("no [route]")

    seen = record(monkeypatch, boom=lifecycle.Transport("boom", frozenset(), deliver))
    two_entries(project, "boom")
    code = lifecycle.run_job("report", "local", [sys.executable, "-c", "raise SystemExit(3)"],
                             project)
    assert code == 3
    assert seen == [("job:run-started", False), ("job:run-failed", False)]
    out, err = capsys.readouterr()
    assert err.count("The boom alert could not send job:run-failed: no [route]") == 1
    assert out.count("[event] not delivered to boom: job:run-failed at=") == 1


def test_a_delivery_past_the_time_limit_falls_back_to_the_console(project, monkeypatch, capsys):
    monkeypatch.setattr(lifecycle, "FLUSH_SECONDS", 0.2)
    release = threading.Event()
    record(monkeypatch, slow=lifecycle.Transport(
        "slow", frozenset(), lambda event, entry, unhandled: release.wait(5)))
    two_entries(project, "slow")
    started = time.monotonic()
    code = lifecycle.run_job("report", "local", [sys.executable, "-c", "raise SystemExit(0)"],
                             project)
    waited = time.monotonic() - started
    release.set()
    assert code == 0
    assert waited < 3
    out = capsys.readouterr().out
    assert "[event] not delivered to slow: job:run-started at=" in out
    assert "[event] not delivered to slow: job:run-completed at=" in out


def test_each_entry_gets_its_events_in_order(project, monkeypatch):
    got = {"a": [], "b": []}

    def deliver(event, entry, unhandled):
        time.sleep(0.01 if entry["exclude-events"] else 0)
        got["a" if entry["exclude-events"] else "b"].append(event.name)

    record(monkeypatch, queued=lifecycle.Transport("queued", frozenset(), deliver))
    (project / "pdt.yml").write_text(
        "alerts:\n  - type: queued\n    events: [all]\n    exclude-events: [app:*]\n"
        "  - type: queued\n    events: [all]\n")
    names = [f"job:event-{n}" for n in range(20)]
    for name in names:
        lifecycle.emit(name)
    lifecycle.flush()
    assert got == {"a": names, "b": names}


def test_run_job_names_the_run_and_its_exit_code(project, monkeypatch):
    seen = []
    monkeypatch.setattr(lifecycle, "emit", lambda name, **params: seen.append((name, params)))
    code = lifecycle.run_job("report", "local", [
        sys.executable, "-c",
        f"import os, sys; sys.exit(3 if os.environ['PDT_RUN_ID'] == {lifecycle.storage.RUN_ID!r} "
        "else 9)"], project)
    assert code == 3
    assert [name for name, _params in seen] == ["job:run-started", "job:run-failed"]
    assert seen[1][1]["exit_code"] == 3
    assert seen[1][1]["run_id"] == seen[0][1]["run_id"]
    assert seen[1][1]["state"] == f"runs/*-{seen[0][1]['run_id']}/"


@pytest.mark.skipif(os.name == "nt", reason="sends a POSIX signal")
@pytest.mark.parametrize(("signum", "code"), [(signal.SIGTERM, 143), (signal.SIGINT, 130)])
def test_a_signal_to_run_job_stops_the_command_and_ends_the_run_cancelled(
        project, monkeypatch, signum, code):
    seen = []
    monkeypatch.setattr(lifecycle, "emit", lambda name, **params: seen.append((name, params)))
    ready = project / "ready"
    command = [sys.executable, "-c",
               f"import pathlib, time; pathlib.Path({str(ready)!r}).touch(); time.sleep(30)"]

    def send():
        while not ready.exists():
            threading.Event().wait(0.05)
        os.kill(os.getpid(), signum)

    before = signal.getsignal(signum)
    threading.Thread(target=send, daemon=True).start()
    assert lifecycle.run_job("report", "local", command, project) == code
    assert seen[-1][0] == "job:run-cancelled"
    assert seen[-1][1]["exit_code"] == code
    assert seen[-1][1]["signal"] == signum.name
    assert signal.getsignal(signum) == before


@pytest.fixture
def deployable(tmp_path, monkeypatch):
    monkeypatch.delenv("PDT_PROJECT", raising=False)
    monkeypatch.chdir(tmp_path)
    assert scaffold.init(None, assume_yes=True) == 0
    (tmp_path / "pdt.yml").write_text("platform:\n  provider: aws\n  region: us-east-1\n")
    seen = []
    monkeypatch.setattr(lifecycle, "emit", lambda name, **params: seen.append((name, params)))
    return seen


def test_ctrl_c_in_a_deploy_ends_it_cancelled(deployable, monkeypatch):
    def dispatch(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(deploy, "dispatch", dispatch)
    with pytest.raises(KeyboardInterrupt):
        deploy.deploy(scaffold.STARTER, assume_yes=True, run=False)
    assert [(name, params.get("reason")) for name, params in deployable] == [
        ("app:deploy-started", None), ("app:deploy-cancelled", "interrupted")]


@pytest.mark.parametrize(("code", "event", "reason"), [
    (deploy.DECLINED, "app:deploy-cancelled", "declined"),
    (130, "app:deploy-cancelled", "interrupted"),
    (1, "app:deploy-failed", None),
])
def test_a_declined_plan_ends_the_deploy_cancelled_and_a_failure_failed(
        deployable, monkeypatch, code, event, reason):
    monkeypatch.setattr(deploy, "dispatch", lambda *args, **kwargs: code)
    assert deploy.deploy(scaffold.STARTER, assume_yes=True, run=False) == code
    assert deployable[-1][0] == event
    assert deployable[-1][1].get("reason") == reason


@pytest.mark.parametrize(("deployed", "override", "trigger"), [
    (False, "", "manual"),
    (False, "schedule", "manual"),
    (True, "manual", "manual"),
    (True, "", "schedule"),
])
def test_a_run_says_if_it_is_deployed_and_what_started_it(project, monkeypatch, deployed,
                                                          override, trigger):
    seen = []
    monkeypatch.setattr(lifecycle, "emit", lambda name, **params: seen.append(params))
    monkeypatch.setenv("PDT_TRIGGER", override)
    lifecycle.run_job("report", "azure", [sys.executable, "-c", ""], project, deployed=deployed)
    assert [(p["provider"], p["deployed"], p["trigger"]) for p in seen] == [
        ("azure", deployed, trigger)] * 2


def test_pdt_run_names_the_configured_provider(project, monkeypatch):
    add_app(project, "report")
    seen = []
    monkeypatch.setattr(lifecycle, "emit", lambda name, **params: seen.append(params))
    monkeypatch.setattr("sys.argv", ["pdt", "run", "report"])
    monkeypatch.setattr(lifecycle.subprocess, "Popen", started(
        lambda command, **kwargs: SimpleNamespace(returncode=0)))
    assert cli.main() == 0
    assert (seen[0]["provider"], seen[0]["deployed"], seen[0]["trigger"]) == (
        "azure", False, "manual")
