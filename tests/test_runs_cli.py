import json
import time
from datetime import UTC, datetime

import pytest

from pdt import runs_cli
from pdt.runs_cli import Line, Run

T0 = datetime(2026, 9, 23, 10, 0, 12, tzinfo=UTC)
T1 = datetime(2026, 9, 23, 10, 0, 24, tzinfo=UTC)
EARLIER = datetime(2026, 9, 22, 10, 0, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def utc_clock(monkeypatch):
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    yield
    time.tzset()


def test_times_render_in_the_local_time_zone(monkeypatch):
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    assert runs_cli.local_text(T0) == "2026-09-23 06:00:12"

NEWEST = Run("stream-2", T0, T1, "succeeded")
OLDER = Run("stream-1", EARLIER, EARLIER.replace(minute=3, second=5), "failed")
LINES = {
    "stream-2": [Line(T0, "DEBUG", "connecting"), Line(T0, "INFO", "fetched 3 rows"),
                 Line(T1, "WARNING", "slow"), Line(T1, "", "pdt: exit 0")],
    "stream-1": [Line(EARLIER, "ERROR", "boom"), Line(EARLIER, "", "Traceback (most recent)"),
                 Line(EARLIER, "INFO", "done"), Line(EARLIER, "", "pdt: exit 1")],
}


def list_two():
    return [NEWEST, OLDER]


def read(run):
    return LINES[run.id]


def test_parse_line_reads_a_json_record():
    line = runs_cli.parse_line('{"severity": "WARNING", "message": "slow", "rows": 3}\n', T0)
    assert line == Line(T0, "WARNING", "slow  rows=3")


def test_parse_line_reads_the_text_form():
    assert runs_cli.parse_line("10:00:12 INFO    fetched  rows=3", T0) == Line(
        T0, "INFO", "fetched  rows=3")
    assert runs_cli.parse_line("10:00:12 ERROR   boom", None) == Line(None, "ERROR", "boom")


def test_parse_line_keeps_a_plain_line_with_no_level():
    assert runs_cli.parse_line("Traceback (most recent call last):", T0) == Line(
        T0, "", "Traceback (most recent call last):")
    assert runs_cli.parse_line("{not json", T0) == Line(T0, "", "{not json")


def test_exit_code_reads_the_marker():
    marker = runs_cli.parse_line("pdt: exit 3", T0)
    assert marker == Line(T0, "", "pdt: exit 3")
    assert runs_cli.exit_code([Line(T0, "INFO", "x"), marker]) == 3
    assert runs_cli.exit_code([Line(T0, "INFO", "x")]) is None


def test_marker_status():
    assert runs_cli.marker_status(LINES["stream-2"], lambda: True) == "succeeded"
    assert runs_cli.marker_status(LINES["stream-1"], lambda: True) == "failed"
    assert runs_cli.marker_status([], lambda: True) == "running"
    assert runs_cli.marker_status([], lambda: False) == "failed"


def test_runs_prints_a_table_newest_first(capsys):
    assert runs_cli.runs(list_two, "my-report", []) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split() == ["#", "Started", "Duration", "Status", "Id"]
    assert lines[1].split() == ["1", "2026-09-23", "10:00:12", "12s", "succeeded", "stream-2"]
    assert lines[2].split() == ["2", "2026-09-22", "10:00:00", "3m", "05s", "failed", "stream-1"]


def test_runs_json_round_trips(capsys):
    assert runs_cli.runs(list_two, "my-report", ["--json"]) == 0
    out = capsys.readouterr().out
    assert json.loads(out)[0] == {"id": "stream-2", "started": T0.isoformat(),
                                  "ended": T1.isoformat(), "status": "succeeded"}
    assert runs_cli.parse_runs("preflight line\n" + out) == [NEWEST, OLDER]


def test_runs_with_no_runs(capsys):
    assert runs_cli.runs(list, "my-report", []) == 0
    assert capsys.readouterr().out == "my-report has not run yet.\n"


def test_logs_shows_the_newest_run_without_the_marker(capsys):
    assert runs_cli.logs(list_two, read, "my-report", []) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "run 1 of my-report: started 2026-09-23 10:00:12, 12s, succeeded"
    assert lines[1] == "10:00:12 DEBUG   connecting"
    assert lines[3] == "10:00:24 WARNING slow"
    assert len(lines) == 4


def test_logs_picks_run_n_and_exits_1_when_it_failed(capsys):
    assert runs_cli.logs(list_two, read, "my-report", ["2"]) == 1
    assert capsys.readouterr().out.startswith("run 2 of my-report:")


def test_logs_failed_picks_the_newest_failed_run(capsys):
    assert runs_cli.logs(list_two, read, "my-report", ["--failed"]) == 1
    assert capsys.readouterr().out.startswith("run 2 of my-report:")


def test_logs_failed_with_no_failed_run(capsys):
    assert runs_cli.logs(lambda: [NEWEST], read, "my-report", ["--failed"]) == 0
    assert capsys.readouterr().out == "my-report has no failed run in its last 1 runs.\n"


def test_logs_errors_keeps_warnings_errors_and_plain_lines(capsys):
    assert runs_cli.logs(list_two, read, "my-report", ["2", "--errors", "--json"]) == 1
    records = json.loads(capsys.readouterr().out)
    assert [record["message"] for record in records] == ["boom", "Traceback (most recent)"]
    assert records[0] == {"time": EARLIER.isoformat(), "level": "ERROR", "message": "boom"}


def test_logs_with_no_runs(capsys):
    assert runs_cli.logs(list, read, "my-report", ["--failed"]) == 0
    assert capsys.readouterr().out == "my-report has not run yet.\n"


def test_logs_out_of_range(capsys):
    assert runs_cli.logs(list_two, read, "my-report", ["3"]) == 1
    assert "pick a number from 1 to 2" in capsys.readouterr().out


def test_health_rows_and_exit_code(capsys):
    running = Run("stream-3", T1, None, "running")
    app_runs = {"a": [NEWEST, OLDER], "b": [OLDER], "c": [running], "d": []}
    assert runs_cli.health(app_runs, False) == 1
    lines = capsys.readouterr().out.splitlines()
    assert lines[1].split() == ["a", "ok", "2026-09-23", "10:00:12", "1", "of", "2", "succeeded"]
    assert lines[2].split() == ["b", "failed", "2026-09-22", "10:00:00", "0", "of", "1",
                                "succeeded"]
    assert lines[3].split() == ["c", "running", "2026-09-23", "10:00:24", "0", "of", "1",
                                "succeeded"]
    assert lines[4].split() == ["d", "not", "yet", "run"]


def test_health_json_and_a_clean_exit(capsys):
    assert runs_cli.health({"a": [NEWEST], "d": []}, True) == 0
    assert json.loads(capsys.readouterr().out) == [
        {"app": "a", "status": "ok", "last_run": T0.isoformat(), "succeeded": 1, "runs": 1},
        {"app": "d", "status": "not yet run", "last_run": None, "succeeded": 0, "runs": 0},
    ]


def test_health_counts_an_unreadable_app_as_a_failure(capsys):
    assert runs_cli.health({"a": None}, False) == 1
    assert "unknown" in capsys.readouterr().out
