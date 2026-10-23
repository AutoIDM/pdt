import json
import subprocess
from datetime import datetime, timezone

import pytest

from pdt import deploy_google_cloud, runs_cli


def test_a_completed_execution_maps_to_succeeded():
    execution = {
        "name": "namespaces/p/executions/pdt-app-abcde",
        "status": {
            "startTime": "2026-09-23T10:00:00Z",
            "completionTime": "2026-09-23T10:00:12Z",
            "conditions": [{"type": "Completed", "status": "True"}],
        },
    }
    run = deploy_google_cloud.execution_run(execution)
    assert run.id == "pdt-app-abcde"
    assert run.started == datetime(2026, 9, 23, 10, 0, 0, tzinfo=timezone.utc)
    assert run.ended == datetime(2026, 9, 23, 10, 0, 12, tzinfo=timezone.utc)
    assert run.status == "succeeded"


def test_a_failed_condition_maps_to_failed():
    execution = {
        "name": "namespaces/p/executions/pdt-app-fghij",
        "status": {
            "startTime": "2026-09-23T10:00:00Z",
            "completionTime": "2026-09-23T10:00:05Z",
            "conditions": [{"type": "Completed", "status": "False"}],
        },
    }
    run = deploy_google_cloud.execution_run(execution)
    assert run.status == "failed"


def test_no_completion_time_means_running():
    execution = {
        "name": "namespaces/p/executions/pdt-app-klmno",
        "status": {"startTime": "2026-09-23T10:00:00Z"},
    }
    run = deploy_google_cloud.execution_run(execution)
    assert run.status == "running"
    assert run.ended is None


def test_an_execution_that_has_not_started_is_running_from_its_creation_time():
    execution = {
        "metadata": {"name": "pdt-app-queued", "creationTimestamp": "2026-09-23T10:00:00Z"},
        "status": {},
    }
    run = deploy_google_cloud.execution_run(execution)
    assert run.id == "pdt-app-queued"
    assert run.status == "running"
    assert run.started == datetime(2026, 9, 23, 10, 0, 0, tzinfo=timezone.utc)
    assert run.ended is None


def test_an_execution_that_failed_before_starting_is_failed_from_its_creation_time():
    execution = {
        "metadata": {"name": "pdt-app-nostart", "creationTimestamp": "2026-09-23T10:00:00Z"},
        "status": {
            "completionTime": "2026-09-23T10:00:03Z",
            "conditions": [{"type": "Completed", "status": "False"}],
        },
    }
    run = deploy_google_cloud.execution_run(execution)
    assert run.status == "failed"
    assert run.started == datetime(2026, 9, 23, 10, 0, 0, tzinfo=timezone.utc)
    assert run.ended == datetime(2026, 9, 23, 10, 0, 3, tzinfo=timezone.utc)


def test_list_runs_sorts_newest_first_and_a_missing_job_is_empty(monkeypatch):
    older = {
        "name": "namespaces/p/executions/pdt-app-old",
        "status": {"startTime": "2026-09-23T09:00:00Z", "completionTime": "2026-09-23T09:00:05Z",
                   "conditions": [{"type": "Completed", "status": "True"}]},
    }
    newer = {
        "name": "namespaces/p/executions/pdt-app-new",
        "status": {"startTime": "2026-09-23T10:00:00Z", "completionTime": "2026-09-23T10:00:05Z",
                   "conditions": [{"type": "Completed", "status": "True"}]},
    }
    monkeypatch.setattr(deploy_google_cloud, "describe_json",
                        lambda *a: [older, newer] if a[0] == "run" else [])
    found = deploy_google_cloud.list_runs("p", "us-central1", "pdt-app")
    assert [run.id for run in found] == ["pdt-app-new", "pdt-app-old"]

    monkeypatch.setattr(deploy_google_cloud, "describe_json", lambda *a: None)
    assert deploy_google_cloud.list_runs("p", "us-central1", "pdt-app") == []


def test_a_redirected_run_list_holds_no_line_of_a_retried_read(monkeypatch, capsys):
    answers = iter([subprocess.CompletedProcess([], 1, "", "ERROR: ABORTED: try again"),
                    subprocess.CompletedProcess([], 0, "[]", "")])
    monkeypatch.setattr(deploy_google_cloud.subprocess, "run", lambda *a, **kw: next(answers))
    monkeypatch.setattr(deploy_google_cloud.time, "sleep", lambda seconds: None)
    assert runs_cli.runs(lambda: deploy_google_cloud.list_runs("p", "us-central1", "pdt-app"),
                         "app", []) == 0
    out, err = capsys.readouterr()
    assert out.splitlines()[0] == "app has not run yet."
    assert "done:" not in out
    assert "done: pdt gcloud run jobs executions list (0s)" in err


def test_list_runs_sorts_an_execution_with_no_start_time_by_its_creation_time(monkeypatch):
    started = {
        "metadata": {"name": "pdt-app-old", "creationTimestamp": "2026-09-23T08:59:58Z"},
        "status": {"startTime": "2026-09-23T09:00:00Z", "completionTime": "2026-09-23T09:00:05Z",
                   "conditions": [{"type": "Completed", "status": "True"}]},
    }
    queued = {
        "metadata": {"name": "pdt-app-queued", "creationTimestamp": "2026-09-23T10:00:00Z"},
        "status": {},
    }
    monkeypatch.setattr(deploy_google_cloud, "describe_json",
                        lambda *a: [started, queued] if a[0] == "run" else [])
    found = deploy_google_cloud.list_runs("p", "us-central1", "pdt-app")
    assert [(run.id, run.status) for run in found] == [("pdt-app-queued", "running"),
                                                        ("pdt-app-old", "succeeded")]


def test_list_runs_reads_every_exit_code_in_one_logging_read(monkeypatch):
    executions = [{
        "name": f"namespaces/p/executions/pdt-app-{suffix}",
        "status": {"startTime": f"2026-09-23T{hour}:00:00Z",
                   "completionTime": f"2026-09-23T{hour}:00:05Z",
                   "conditions": [{"type": "Completed", "status": "True"}]},
    } for suffix, hour in (("new", "10"), ("mid", "09"), ("old", "08"))]
    markers = [
        {"labels": {"run.googleapis.com/execution_name": "pdt-app-new"},
         "textPayload": "pdt: exit 0"},
        {"labels": {"run.googleapis.com/execution_name": "pdt-app-mid"},
         "textPayload": "pdt: exit 2"},
    ]
    calls = []

    def describe_json(*args):
        calls.append(args)
        return executions if args[0] == "run" else markers

    monkeypatch.setattr(deploy_google_cloud, "describe_json", describe_json)
    found = deploy_google_cloud.list_runs("p", "us-central1", "pdt-app")
    assert [run.exit_code for run in found] == [0, 2, None]
    [logging_call] = [call for call in calls if call[0] == "logging"]
    assert 'resource.labels.job_name="pdt-app"' in logging_call[2]
    assert 'textPayload:"pdt: exit "' in logging_call[2]
    assert logging_call[logging_call.index("--limit") + 1] == "3"
    [list_call] = [call for call in calls if call[0] == "run"]
    assert "--limit" not in list_call


def test_read_lines_builds_from_json_and_text_payloads(monkeypatch):
    entries = [
        {"timestamp": "2026-09-23T10:00:01Z", "severity": "INFO",
         "jsonPayload": {"message": "starting", "rows": 3}},
        {"timestamp": "2026-09-23T10:00:02Z", "textPayload": "10:00:02 ERROR   boom"},
    ]
    fake_logging(monkeypatch, entries)
    lines = deploy_google_cloud.read_lines("p", "pdt-app-abcde")
    assert lines == [
        runs_cli.Line(datetime(2026, 9, 23, 10, 0, 1, tzinfo=timezone.utc), "INFO", "starting  rows=3"),
        runs_cli.Line(datetime(2026, 9, 23, 10, 0, 2, tzinfo=timezone.utc), "ERROR", "boom"),
    ]


def fake_logging(monkeypatch, entries, code=0, stderr=""):
    calls = []

    def gcloud(*args):
        calls.append(args)
        return subprocess.CompletedProcess(args, code, json.dumps(entries), stderr)

    monkeypatch.setattr(deploy_google_cloud, "gcloud", gcloud)
    return calls


def test_read_lines_with_no_entries_is_empty(monkeypatch):
    fake_logging(monkeypatch, [])
    assert deploy_google_cloud.read_lines("p", "pdt-app-abcde") == []


def test_a_failed_read_warns_instead_of_looking_empty(monkeypatch, capsys):
    fake_logging(monkeypatch, None, 1, "ERROR: (gcloud.logging.read) RESOURCE_EXHAUSTED: Quota exceeded")
    assert deploy_google_cloud.read_lines("p", "pdt-app-abcde") == []
    assert deploy_google_cloud.read_lines("p", "pdt-app-abcde") == []
    assert capsys.readouterr().out.count("RESOURCE_EXHAUSTED: Quota exceeded") == 1


def test_read_lines_reads_every_entry_of_the_container_logs(monkeypatch):
    calls = fake_logging(monkeypatch, [
        {"timestamp": "2026-09-23T10:00:01Z", "textPayload": f"line {number}"}
        for number in range(6000)])
    assert len(deploy_google_cloud.read_lines("p", "pdt-app-abcde")) == 6000
    [call] = calls
    assert "--limit" not in call
    assert 'log_id("run.googleapis.com/stdout") OR log_id("run.googleapis.com/stderr")' in call[2]
    assert "timestamp" not in call[2]


def test_read_lines_from_a_time_asks_only_for_the_entries_from_it(monkeypatch):
    calls = fake_logging(monkeypatch, [])
    deploy_google_cloud.read_lines("p", "pdt-app-abcde",
                                   datetime(2026, 9, 23, 10, 0, 1, tzinfo=timezone.utc))
    [call] = calls
    assert call[2].endswith(' AND timestamp>="2026-09-23T10:00:01+00:00"')


STORED = {"timestamp": "2026-09-23T10:00:01Z", "severity": "INFO",
          "jsonPayload": {"message": "starting", "rows": 3}}
# The same entry from a tail, as json_format.MessageToDict gives it.
TAILED = {"timestamp": "2026-09-23T10:00:01Z", "severity": "INFO",
          "jsonPayload": {"message": "starting", "rows": 3.0}}


def test_a_tail_entry_gives_the_line_the_store_gives():
    assert deploy_google_cloud.entry_line(TAILED) == deploy_google_cloud.entry_line(STORED)
    assert deploy_google_cloud.entry_line(TAILED).message == "starting  rows=3"
    half = {**TAILED, "jsonPayload": {"message": "ratio", "share": 2.5}}
    assert deploy_google_cloud.entry_line(half).message == "ratio  share=2.5"


def test_follow_streams_the_store_then_each_new_line_of_the_tail(monkeypatch):
    calls = fake_logging(monkeypatch, [STORED])
    exit_entry = {"timestamp": "2026-09-23T10:00:02Z", "textPayload": "pdt: exit 0"}

    stops = []

    def tail_entries(project, log_filter, stop):
        calls.append(("tail", project, log_filter))
        stops.append(stop)
        return iter([TAILED, exit_entry])

    monkeypatch.setattr(deploy_google_cloud, "tail_entries", tail_entries)
    run = runs_cli.Run("pdt-app-abcde", datetime(2026, 9, 23, 10, tzinfo=timezone.utc), None,
                       "running")
    lines = list(deploy_google_cloud.stream_lines("p", run, True))
    assert [line.message for line in lines] == ["starting  rows=3", "pdt: exit 0"]
    # The tail opens before the first store read, so no line falls between them.
    assert calls[0] == ("tail", "p", deploy_google_cloud.execution_filter("pdt-app-abcde"))
    assert calls[1][:2] == ("logging", "read")
    assert stops[0].is_set()


def test_without_follow_the_stream_reads_the_store_only(monkeypatch):
    fake_logging(monkeypatch, [STORED])

    def tail_entries(project, log_filter, stop):
        raise AssertionError("a snapshot opens no tail")

    monkeypatch.setattr(deploy_google_cloud, "tail_entries", tail_entries)
    run = runs_cli.Run("pdt-app-abcde", datetime(2026, 9, 23, 10, tzinfo=timezone.utc), None,
                       "running")
    assert [line.message for line in deploy_google_cloud.stream_lines("p", run, False)] == [
        "starting  rows=3"]


def test_logs_list_names_the_filter_once_and_each_execution(monkeypatch, capsys):
    found = [runs_cli.Run("pdt-app-b", datetime(2026, 9, 23, 11, tzinfo=timezone.utc), None,
                          "running"),
             runs_cli.Run("pdt-app-a", datetime(2026, 9, 23, 10, tzinfo=timezone.utc),
                          datetime(2026, 9, 23, 10, 1, tzinfo=timezone.utc), "failed", 2)]
    monkeypatch.setattr(deploy_google_cloud, "project_region", lambda app: ("p", "r"))
    monkeypatch.setattr(deploy_google_cloud, "preflight", lambda app, project, yes: project)
    monkeypatch.setattr(deploy_google_cloud, "list_runs", lambda project, region, job: found)
    monkeypatch.setattr(deploy_google_cloud, "read_lines",
                        lambda *args: pytest.fail("read a log"))
    assert deploy_google_cloud.logs({"name": "app"}, ["--list", "--json"], False) == 0
    records = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert [(record["number"], record["location"], record["exit_code"]) for record in records] == [
        (1, "pdt-app-b", None), (2, "pdt-app-a", 2)]
    assert deploy_google_cloud.logs({"name": "app"}, ["--list"], False) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "Cloud Logging: project p, by execution name"
    assert lines[2].split()[:2] == ["1", "pdt-app-b"]
    assert lines[3].split()[-1] == "2"
