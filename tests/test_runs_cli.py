import json
import time
from datetime import UTC, datetime, timedelta

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

NEWEST = Run("stream-2", T0, T1, "succeeded", 0)
OLDER = Run("stream-1", EARLIER, EARLIER.replace(minute=3, second=5), "failed", 1)
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


def test_parse_line_reads_a_meltano_line_with_its_own_time_and_level():
    line = runs_cli.parse_line(
        "2026-09-23T13:00:35.009777Z [warning  ] dbt          Deprecated functionality", T0)
    assert line.time == datetime(2026, 9, 23, 13, 0, 35, 9777, tzinfo=UTC)
    assert line.level == "WARNING"
    assert line.message == "dbt          Deprecated functionality"


def test_lines_sort_by_their_own_time_and_a_traceback_follows_its_line():
    late = Line(T1, "INFO", "late")
    early = Line(T0, "ERROR", "early")
    trace = Line(None, "", "  File run.py")
    assert runs_cli.in_time_order([late, early, trace]) == [early, trace, late]
    assert runs_cli.in_time_order([late, trace, early]) == [early, late, trace]


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
    assert lines[0].split() == ["#", "Started", "Duration", "Status", "Exit", "Id"]
    assert lines[1].split() == ["1", "2026-09-23", "10:00:12", "12s", "succeeded", "0",
                                "stream-2"]
    assert lines[2].split() == ["2", "2026-09-22", "10:00:00", "3m", "05s", "failed", "1",
                                "stream-1"]


def test_runs_shows_a_dash_for_an_unknown_exit_code(capsys):
    running = Run("stream-3", T1, None, "running")
    assert runs_cli.runs(lambda: [running], "my-report", []) == 0
    assert capsys.readouterr().out.splitlines()[1].split() == [
        "1", "2026-09-23", "10:00:24", "-", "running", "-", "stream-3"]


def test_runs_json_round_trips(capsys):
    assert runs_cli.runs(list_two, "my-report", ["--json"]) == 0
    out = capsys.readouterr().out
    assert json.loads(out)[0] == {"id": "stream-2", "started": T0.isoformat(),
                                  "ended": T1.isoformat(), "status": "succeeded",
                                  "exit_code": 0}
    assert runs_cli.parse_runs("preflight line\n" + out) == [NEWEST, OLDER]


def hourly_runs(count: int) -> list[Run]:
    return [Run(f"run-{hours}", T0 - timedelta(hours=hours),
                T0 - timedelta(hours=hours) + timedelta(seconds=5), "succeeded", 0)
            for hours in range(count)]


def test_parse_since_reads_a_count_with_a_unit():
    assert runs_cli.parse_since("12h", T0) == T0 - timedelta(hours=12)
    assert runs_cli.parse_since("3d", T0) == T0 - timedelta(days=3)
    assert runs_cli.parse_since("2w", T0) == T0 - timedelta(weeks=2)


def test_parse_since_reads_a_date_and_a_date_time_in_local_time(monkeypatch):
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    assert runs_cli.parse_since("2026-09-20", T0) == datetime(2026, 9, 20, 4, 0, tzinfo=UTC)
    assert runs_cli.parse_since("2026-09-20T14:00", T0) == datetime(2026, 9, 20, 18, 0,
                                                                    tzinfo=UTC)


@pytest.mark.parametrize("value", ["3", "3m", "d", "yesterday", "2026-09-20 14:00",
                                   "2026-09-20T14:00:00"])
def test_parse_since_rejects_other_forms(value):
    with pytest.raises(ValueError, match="12h, 3d, 2w"):
        runs_cli.parse_since(value, T0)


def test_runs_shows_the_10_newest_by_default(capsys):
    assert runs_cli.runs(lambda: hourly_runs(12), "my-report", []) == 0
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 1 + runs_cli.DEFAULT_RUNS
    assert lines[-1].split()[-1] == "run-9"


def test_runs_since_keeps_the_runs_at_or_after_the_moment(capsys):
    assert runs_cli.runs(lambda: hourly_runs(12), "my-report",
                         ["--since", "2026-09-23T00:00"]) == 0
    ids = [line.split()[-1] for line in capsys.readouterr().out.splitlines()[1:]]
    assert ids == [f"run-{hours}" for hours in range(11)]


def test_runs_json_returns_every_fetched_run(capsys):
    assert runs_cli.runs(lambda: hourly_runs(12), "my-report", ["--json"]) == 0
    assert len(json.loads(capsys.readouterr().out)) == 12


def test_runs_since_with_no_run_in_the_window(capsys):
    assert runs_cli.runs(lambda: [OLDER], "my-report", ["--since", "1h"]) == 0
    assert capsys.readouterr().out == "my-report has not run since 1h.\n"


def test_runs_with_a_bad_since_value_names_the_forms(capsys):
    assert runs_cli.runs(lambda: pytest.fail("listed runs"), "my-report",
                         ["--since", "soon"]) == 1
    assert "2026-09-20T14:00" in capsys.readouterr().out


def test_logs_numbers_runs_within_the_since_window(capsys):
    since = "2026-09-23T09:00"
    found = [NEWEST, Run("stream-0", T0 - timedelta(minutes=30), T0, "failed", 1), OLDER]
    assert runs_cli.logs(lambda: found, lambda run: [], "my-report",
                         ["2", "--since", since]) == 1
    assert capsys.readouterr().out.startswith("run 2 of my-report:")
    assert runs_cli.logs(lambda: found, lambda run: [], "my-report",
                         ["3", "--since", since]) == 1
    assert "pick a number from 1 to 2" in capsys.readouterr().out


def test_logs_indexes_the_10_newest_without_since(capsys):
    assert runs_cli.logs(lambda: hourly_runs(12), lambda run: [], "my-report", ["11"]) == 1
    assert "pick a number from 1 to 10" in capsys.readouterr().out


def test_runs_with_no_runs(capsys):
    assert runs_cli.runs(list, "my-report", []) == 0
    assert capsys.readouterr().out == "my-report has not run yet.\n"


def test_logs_shows_the_newest_run_without_the_marker(capsys):
    assert runs_cli.logs(list_two, read, "my-report", []) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "run 1 of my-report: started 2026-09-23 10:00:12, 12s, succeeded, exit 0"
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


def test_health_counts_the_10_newest_runs(capsys):
    assert runs_cli.health({"a": hourly_runs(12)}, True) == 0
    [row] = json.loads(capsys.readouterr().out)
    assert (row["succeeded"], row["runs"]) == (10, 10)


def test_health_counts_an_unreadable_app_as_a_failure(capsys):
    assert runs_cli.health({"a": None}, False) == 1
    assert "unknown" in capsys.readouterr().out
