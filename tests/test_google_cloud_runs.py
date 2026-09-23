from datetime import datetime, timezone

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
    monkeypatch.setattr(deploy_google_cloud, "describe_json", lambda *a: [older, newer])
    found = deploy_google_cloud.list_runs("p", "us-central1", "pdt-app")
    assert [run.id for run in found] == ["pdt-app-new", "pdt-app-old"]

    monkeypatch.setattr(deploy_google_cloud, "describe_json", lambda *a: None)
    assert deploy_google_cloud.list_runs("p", "us-central1", "pdt-app") == []


def test_read_lines_builds_from_json_and_text_payloads(monkeypatch):
    entries = [
        {"timestamp": "2026-09-23T10:00:01Z", "severity": "INFO",
         "jsonPayload": {"message": "starting", "rows": 3}},
        {"timestamp": "2026-09-23T10:00:02Z", "textPayload": "10:00:02 ERROR   boom"},
    ]
    monkeypatch.setattr(deploy_google_cloud, "describe_json", lambda *a: entries)
    lines = deploy_google_cloud.read_lines("p", "pdt-app-abcde")
    assert lines == [
        runs_cli.Line(datetime(2026, 9, 23, 10, 0, 1, tzinfo=timezone.utc), "INFO", "starting  rows=3"),
        runs_cli.Line(datetime(2026, 9, 23, 10, 0, 2, tzinfo=timezone.utc), "ERROR", "boom"),
    ]


def test_read_lines_with_no_entries_is_empty(monkeypatch):
    monkeypatch.setattr(deploy_google_cloud, "describe_json", lambda *a: None)
    assert deploy_google_cloud.read_lines("p", "pdt-app-abcde") == []
