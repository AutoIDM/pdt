from datetime import datetime, timedelta, timezone

import pytest

from pdt import deploy_snowflake, runs_cli
from pdt.deploy_snowflake import Session

NAMES = deploy_snowflake.names("hello-world")
EVENTS = "SNOWFLAKE.TELEMETRY.EVENTS"
EVENT_TABLE = [{"name": "EVENT_TABLE", "value": EVENTS}]
SCHEMA_FOUND = [
    ("SHOW DATABASES", [{"name": "PDT"}]),
    ("SHOW SCHEMAS", [{"name": "PDT_HELLO_WORLD"}]),
]


def session(has_warehouse=True):
    return Session(conn=object(), account="myorg-myacct", region="AWS_US_EAST_1", role="SYSADMIN",
                   user="jon", has_warehouse=has_warehouse)


def record_sql(monkeypatch, answers):
    calls = []

    def fake_run(session, sql, file_stream=None):
        calls.append(sql)
        for prefix, rows in answers:
            if sql.startswith(prefix):
                return rows
        return []

    monkeypatch.setattr(deploy_snowflake, "run", fake_run)
    return calls


def job(name, status, created, updated):
    return {"name": name, "status": status, "created_on": created, "updated_on": updated}


def test_a_done_job_service_maps_to_succeeded_in_utc():
    west = timezone(timedelta(hours=-7))
    row = job("RUN_20260929_100000", "DONE", datetime(2026, 9, 29, 3, 0, 0, tzinfo=west),
              datetime(2026, 9, 29, 3, 0, 12, tzinfo=west))
    run = deploy_snowflake.service_run(row)
    assert run.id == "RUN_20260929_100000"
    assert run.started == datetime(2026, 9, 29, 10, 0, 0, tzinfo=timezone.utc)
    assert run.ended == datetime(2026, 9, 29, 10, 0, 12, tzinfo=timezone.utc)
    assert run.status == "succeeded"


def test_a_failed_job_service_maps_to_failed_from_iso_strings():
    row = job("RUN_20260929_100000", "FAILED", "2026-09-29T10:00:00+00:00",
              "2026-09-29T10:00:05+00:00")
    run = deploy_snowflake.service_run(row)
    assert run.status == "failed"
    assert run.ended == datetime(2026, 9, 29, 10, 0, 5, tzinfo=timezone.utc)


@pytest.mark.parametrize("status", ["RUNNING", "PENDING"])
def test_a_job_service_still_in_progress_is_running_with_no_end(status):
    row = job("RUN_20260929_100000", status, "2026-09-29T10:00:00+00:00",
              "2026-09-29T10:00:03+00:00")
    run = deploy_snowflake.service_run(row)
    assert run.status == "running"
    assert run.ended is None


def test_list_runs_sorts_newest_first_and_skips_other_services(monkeypatch):
    record_sql(monkeypatch, [
        *SCHEMA_FOUND,
        ("SHOW JOB SERVICES", [
            job("RUN_20260929_010101", "DONE", "2026-09-29T01:01:01+00:00", "2026-09-29T01:01:09+00:00"),
            job("OTHER_JOB", "DONE", "2026-09-29T05:00:00+00:00", "2026-09-29T05:00:09+00:00"),
            job("RUN_20260929_030303", "DONE", "2026-09-29T03:03:03+00:00", "2026-09-29T03:03:09+00:00"),
        ]),
    ])
    found = deploy_snowflake.list_runs(session(has_warehouse=False), NAMES)
    assert [run.id for run in found] == ["RUN_20260929_030303", "RUN_20260929_010101"]


def test_list_runs_of_a_missing_schema_is_empty(monkeypatch):
    calls = record_sql(monkeypatch, [("SHOW DATABASES", [{"name": "PDT"}])])
    assert deploy_snowflake.list_runs(session(), NAMES) == []
    assert not any(call.startswith("SHOW JOB SERVICES") for call in calls)


def test_list_runs_reads_every_exit_code_in_one_event_table_query(monkeypatch):
    calls = record_sql(monkeypatch, [
        *SCHEMA_FOUND,
        ("SHOW JOB SERVICES", [
            job("RUN_20260929_030303", "DONE", "2026-09-29T03:03:03+00:00", "2026-09-29T03:03:09+00:00"),
            job("RUN_20260929_010101", "FAILED", "2026-09-29T01:01:01+00:00", "2026-09-29T01:01:09+00:00"),
        ]),
        ("SHOW PARAMETERS", EVENT_TABLE),
        ("SELECT RESOURCE_ATTRIBUTES", [
            {"service": "RUN_20260929_030303", "message": "pdt: exit 0"},
            {"service": "RUN_20260929_010101", "message": "pdt: exit 2"},
        ]),
    ])
    found = deploy_snowflake.list_runs(session(), NAMES)
    assert [run.exit_code for run in found] == [0, 2]
    [query] = [call for call in calls if call.startswith("SELECT RESOURCE_ATTRIBUTES")]
    assert f"FROM {EVENTS}" in query
    assert "LIKE 'pdt: exit %'" in query
    assert query.endswith("LIMIT 2")


def test_list_runs_without_an_event_table_leaves_the_exit_codes_empty(monkeypatch):
    calls = record_sql(monkeypatch, [
        *SCHEMA_FOUND,
        ("SHOW JOB SERVICES", [
            job("RUN_20260929_010101", "DONE", "2026-09-29T01:01:01+00:00", "2026-09-29T01:01:09+00:00"),
        ]),
    ])
    [run] = deploy_snowflake.list_runs(session(), NAMES)
    assert run.exit_code is None
    assert not any(call.startswith("SELECT RESOURCE_ATTRIBUTES") for call in calls)


def test_read_lines_builds_from_json_and_text_messages_in_utc(monkeypatch):
    calls = record_sql(monkeypatch, [
        ("SHOW PARAMETERS", EVENT_TABLE),
        ("SELECT TIMESTAMP", [
            {"timestamp": datetime(2026, 9, 29, 10, 0, 1),
             "message": '{"severity": "INFO", "message": "starting", "rows": 3}'},
            {"timestamp": datetime(2026, 9, 29, 10, 0, 2), "message": "10:00:02 ERROR   boom"},
        ]),
    ])
    lines = deploy_snowflake.read_lines(session(), NAMES, "RUN_20260929_010101")
    assert lines == [
        runs_cli.Line(datetime(2026, 9, 29, 10, 0, 1, tzinfo=timezone.utc), "INFO", "starting  rows=3"),
        runs_cli.Line(datetime(2026, 9, 29, 10, 0, 2, tzinfo=timezone.utc), "ERROR", "boom"),
    ]
    assert "\"snow.service.name\" = 'RUN_20260929_010101'" in calls[-1]


def test_logs_are_unreadable_without_the_warehouse():
    table, reason = deploy_snowflake.logs_readable(session(has_warehouse=False))
    assert table == ""
    assert "warehouse PDT is missing" in reason


def test_logs_are_unreadable_without_an_event_table_and_the_reason_gives_the_fix(monkeypatch):
    record_sql(monkeypatch, [("SHOW PARAMETERS", [{"name": "EVENT_TABLE", "value": ""}])])
    table, reason = deploy_snowflake.logs_readable(session())
    assert table == ""
    assert "ALTER ACCOUNT SET EVENT_TABLE = SNOWFLAKE.TELEMETRY.EVENTS" in reason


def test_logs_are_readable_from_the_account_event_table(monkeypatch):
    record_sql(monkeypatch, [("SHOW PARAMETERS", EVENT_TABLE)])
    assert deploy_snowflake.logs_readable(session()) == (EVENTS, "")


def test_read_lines_fails_with_the_reason_when_logs_are_unreadable():
    with pytest.raises(SystemExit):
        deploy_snowflake.read_lines(session(has_warehouse=False), NAMES, "RUN_20260929_010101")
