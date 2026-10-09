import datetime
import json

import pytest

from pdt import deploy_azure, deploy_azure_container_apps, runs_cli

SETTINGS = {
    "resource_group": "pdt", "workspace": "pdt-logs", "suffix": "abc1234def",
    "environment": deploy_azure.Environment("pdt-shared", "pdt-eastus", True),
}


def execution(name: str, status: str, start: str, end: str | None) -> dict:
    props = {"status": status, "startTime": start}
    if end:
        props["endTime"] = end
    return {"name": name, "properties": props}


def fake_az(monkeypatch, execs, marker_rows=()):
    calls = []

    def az_json(*args):
        calls.append(args)
        if args[0] == "containerapp":
            return execs
        return {"tables": [{"rows": [list(row) for row in marker_rows]}]}

    monkeypatch.setattr(deploy_azure_container_apps, "az_json", az_json)
    monkeypatch.setattr(deploy_azure_container_apps, "az_tsv", lambda *args: "workspace-id")
    return calls


def test_status_mapping_and_times(monkeypatch):
    execs = [
        execution("job-a", "Succeeded", "2026-09-23T10:00:00Z", "2026-09-23T10:00:12Z"),
        execution("job-b", "Running", "2026-09-23T10:05:00Z", None),
        execution("job-c", "Processing", "2026-09-23T10:06:00Z", None),
        execution("job-d", "Degraded", "2026-09-23T09:00:00Z", "2026-09-23T09:00:05Z"),
    ]
    fake_az(monkeypatch, execs)
    found = deploy_azure_container_apps.list_runs(SETTINGS, "pdt-report")
    by_id = {run.id: run for run in found}
    assert by_id["job-a"].status == "succeeded"
    assert by_id["job-b"].status == "running"
    assert by_id["job-c"].status == "running"
    assert by_id["job-d"].status == "failed"
    assert by_id["job-a"].started == datetime.datetime.fromisoformat("2026-09-23T10:00:00+00:00")
    assert by_id["job-a"].ended == datetime.datetime.fromisoformat("2026-09-23T10:00:12+00:00")
    assert by_id["job-b"].ended is None
    assert [run.id for run in found] == ["job-c", "job-b", "job-a", "job-d"]


def test_a_missing_job_has_no_runs(monkeypatch):
    calls = fake_az(monkeypatch, None)
    assert deploy_azure_container_apps.list_runs(SETTINGS, "pdt-report") == []
    assert [call[0] for call in calls] == ["containerapp"]


def test_list_runs_keeps_every_execution(monkeypatch):
    execs = [execution(f"job-{minute:02d}", "Succeeded", f"2026-09-23T10:{minute:02d}:00Z",
                       f"2026-09-23T10:{minute:02d}:05Z") for minute in range(60)]
    fake_az(monkeypatch, execs)
    assert len(deploy_azure_container_apps.list_runs(SETTINGS, "pdt-report")) == 60


def test_list_runs_reads_every_exit_code_in_one_query(monkeypatch):
    execs = [
        execution("pdt-report-aaa", "Succeeded", "2026-09-23T10:00:00Z", "2026-09-23T10:00:12Z"),
        execution("pdt-report-bbb", "Failed", "2026-09-23T09:00:00Z", "2026-09-23T09:00:05Z"),
        execution("pdt-report-ccc", "Succeeded", "2026-09-23T08:00:00Z", "2026-09-23T08:00:05Z"),
    ]
    calls = fake_az(monkeypatch, execs, [
        ("pdt-report-aaa", "Container 'pdt-report' was terminated with exit code '0' and reason 'ProcessExited'"),
        ("pdt-report-bbb", "Container 'pdt-report' was terminated with exit code '3' and reason 'ProcessExited'")])
    found = deploy_azure_container_apps.list_runs(SETTINGS, "pdt-report")
    assert [run.exit_code for run in found] == [0, 3, None]
    [query_call] = [call for call in calls if call[0] == "rest"]
    query = json.loads(query_call[query_call.index("--body") + 1])["query"]
    assert "ContainerAppSystemLogs_CL | where JobName_s == 'pdt-report'" in query
    assert "Reason_s == 'ContainerTerminated'" in query


def test_read_lines_queries_the_shared_workspace(monkeypatch):
    calls = []

    def az_tsv(*args):
        calls.append(args)
        return "workspace-customer-id"

    def az_json(*args):
        calls.append(args)
        return {"tables": [{"columns": [{"name": "TimeGenerated"}, {"name": "Log_s"},
                                        {"name": "ContainerGroupName_s"}],
                            "rows": [["2026-09-23T10:00:01.1234567Z", "10:00:01 INFO    hello",
                                      "job-a-x1"]]}]}

    monkeypatch.setattr(deploy_azure_container_apps, "az_tsv", az_tsv)
    monkeypatch.setattr(deploy_azure_container_apps, "az_json", az_json)
    lines = deploy_azure_container_apps.read_lines(SETTINGS, "pdt-report", "job-a")
    assert lines == [runs_cli.Line(
        datetime.datetime(2026, 9, 23, 10, 0, 1, 123456, tzinfo=datetime.UTC), "INFO", "hello")]
    workspace_call = calls[0]
    assert workspace_call[:3] == ("monitor", "log-analytics", "workspace")
    assert workspace_call[workspace_call.index("--resource-group") + 1] == "pdt-shared"
    assert workspace_call[workspace_call.index("--workspace-name") + 1] == "pdt-logs"
    query_call = calls[1]
    assert query_call[query_call.index("--url") + 1] == (
        "https://api.loganalytics.io/v1/workspaces/workspace-customer-id/query")
    query = json.loads(query_call[query_call.index("--body") + 1])["query"]
    assert "ContainerJobName_s == 'pdt-report'" in query
    assert "ContainerGroupName_s startswith 'job-a'" in query


def test_logs_names_the_ingestion_delay_for_a_run_that_ended_moments_ago(monkeypatch, capsys):
    end = datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=1)
    start = end - datetime.timedelta(seconds=12)
    fake_az(monkeypatch, [execution("job-a", "Succeeded", start.isoformat(), end.isoformat())])
    monkeypatch.setattr(deploy_azure_container_apps, "read_lines", lambda *args: [])
    assert deploy_azure_container_apps.logs({"name": "report"}, SETTINGS, []) == 0
    out = capsys.readouterr().out
    assert "Azure Log Analytics can take up to 5 minutes to show a line, so no lines are" in out
    assert "pdt logs report 1 --follow" in out


def test_logs_says_plainly_when_an_old_run_has_no_lines(monkeypatch, capsys):
    ended = execution("job-a", "Succeeded", "2026-09-23T10:00:00Z", "2026-09-23T10:00:12Z")
    fake_az(monkeypatch, [ended])
    monkeypatch.setattr(deploy_azure_container_apps, "read_lines", lambda *args: [])
    assert deploy_azure_container_apps.logs({"name": "report"}, SETTINGS, []) == 0
    assert "Azure Log Analytics has no lines from run 1." in capsys.readouterr().out


def test_read_many_asks_log_analytics_once_and_splits_the_rows(monkeypatch):
    queries = []

    def fake_query(settings, query):
        queries.append(query)
        return [["2026-09-23T10:00:00Z", "one", "pdt-job-aaa-x1"],
                ["2026-09-23T10:00:01Z", "two", "pdt-job-bbb-x2"],
                ["2026-09-23T10:00:02Z", "three", "pdt-job-aaa-x1"]]

    monkeypatch.setattr(deploy_azure_container_apps, "log_query", fake_query)
    lines = deploy_azure_container_apps.read_many(SETTINGS, "pdt-job", ["pdt-job-aaa", "pdt-job-bbb"])
    assert len(queries) == 1
    assert "ContainerGroupName_s startswith 'pdt-job-aaa' or ContainerGroupName_s startswith 'pdt-job-bbb'" in queries[0]
    assert [line.message for line in lines["pdt-job-aaa"]] == ["one", "three"]
    assert [line.message for line in lines["pdt-job-bbb"]] == ["two"]
    assert deploy_azure_container_apps.read_many(SETTINGS, "pdt-job", []) == {}


def test_read_many_orders_lines_by_the_time_the_container_wrote_them(monkeypatch):
    queries = []
    monkeypatch.setattr(deploy_azure_container_apps, "log_query",
                        lambda settings, query: queries.append(query) or [])
    deploy_azure_container_apps.read_many(SETTINGS, "pdt-job", ["pdt-job-aaa"])
    assert "extend written = coalesce(time_t, TimeGenerated)" in queries[0]
    assert queries[0].endswith("| order by written asc")


STREAM_SETTINGS = {**SETTINGS, "subscription": "123456789012"}
JOB = {"properties": {
    "eventStreamEndpoint": "https://eastus2.azurecontainerapps.dev/subscriptions/123456789012"
                           "/resourceGroups/pdt/containerApps/pdt-report/eventstream",
    "template": {"containers": [{"name": "pdt-report"}]}}}
RUNNING_RUN = runs_cli.Run("pdt-report-x1", datetime.datetime(2026, 10, 9, 21, 15, tzinfo=datetime.UTC),
                           None, "running")


class Response:
    def __init__(self, body: bytes):
        self.rows = body.splitlines(keepends=True)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def __iter__(self):
        return iter(self.rows)


def fake_stream_api(monkeypatch, replicas, body=b"", calls=None):
    def az_json(*args):
        url = args[args.index("--url") + 1]
        if "/replicas?" in url:
            return replicas
        if "/getAuthToken?" in url:
            return {"properties": {"token": "stream-token"}}
        return JOB

    def urlopen(request, timeout):
        if calls is not None:
            calls.append((request.full_url, request.get_header("Authorization"), timeout))
        if isinstance(body, Exception):
            raise body
        return Response(body)

    monkeypatch.setattr(deploy_azure_container_apps, "az_json", az_json)
    monkeypatch.setattr(deploy_azure_container_apps.urllib.request, "urlopen", urlopen)


def test_stream_lines_reads_the_container_log_with_its_own_times(monkeypatch):
    calls = []
    body = (b"2026-10-09T21:16:02.38246  Connecting to the container 'pdt-report'...\n"
            b"2026-10-09T21:15:44.4795058Z stdout F line 1\n"
            b"2026-10-09T21:15:44.4795506Z stderr F {\"severity\": \"WARNING\", \"message\": \"slow\"}\n"
            b"2026-10-09T21:15:45.0000000Z stdout F \n")
    fake_stream_api(monkeypatch, {"value": [{"name": "pdt-report-x1-abc"}]}, body, calls)
    lines = list(deploy_azure_container_apps.stream_lines(STREAM_SETTINGS, "pdt-report",
                                                          RUNNING_RUN, True))
    assert [(line.level, line.message) for line in lines] == [
        ("", "line 1"), ("WARNING", "slow"), ("", "")]
    assert lines[0].time == datetime.datetime(2026, 10, 9, 21, 15, 44, 479505, tzinfo=datetime.UTC)
    [(url, authorization, _timeout)] = calls
    assert url == ("https://eastus2.azurecontainerapps.dev/subscriptions/123456789012"
                   "/resourceGroups/pdt/jobs/pdt-report/executions/pdt-report-x1"
                   "/replicas/pdt-report-x1-abc/containers/pdt-report/logstream?follow=true")
    assert authorization == "Bearer stream-token"


def test_stream_lines_of_an_execution_with_no_container_yet_is_empty(monkeypatch):
    calls = []
    fake_stream_api(monkeypatch, {"value": []}, calls=calls)
    assert list(deploy_azure_container_apps.stream_lines(STREAM_SETTINGS, "pdt-report",
                                                         RUNNING_RUN, False)) == []
    assert calls == []


def test_a_quiet_stream_ends_so_follow_opens_it_again(monkeypatch):
    fake_stream_api(monkeypatch, {"value": [{"name": "r"}]}, TimeoutError("timed out"))
    assert list(deploy_azure_container_apps.stream_lines(STREAM_SETTINGS, "pdt-report",
                                                         RUNNING_RUN, True)) == []


def test_a_stream_that_azure_refuses_is_a_stream_error(monkeypatch):
    fake_stream_api(monkeypatch, None)
    with pytest.raises(runs_cli.StreamError, match="pdt-report"):
        list(deploy_azure_container_apps.stream_lines(STREAM_SETTINGS, "pdt-report",
                                                      RUNNING_RUN, True))
    fake_stream_api(monkeypatch, {"value": [{"name": "r"}]}, ConnectionResetError("reset"))
    with pytest.raises(runs_cli.StreamError, match="reset"):
        list(deploy_azure_container_apps.stream_lines(STREAM_SETTINGS, "pdt-report",
                                                      RUNNING_RUN, True))
