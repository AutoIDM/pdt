import dataclasses
import io
import json
import time
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta

import pytest
from rich.console import Console

from pdt import console, runs_cli
from pdt.runs_cli import Line, Run

pytestmark = pytest.mark.skipif(not hasattr(time, "tzset"), reason="needs time.tzset to set TZ")

T0 = datetime(2026, 9, 23, 10, 0, 12, tzinfo=UTC)
T1 = datetime(2026, 9, 23, 10, 0, 24, tzinfo=UTC)
EARLIER = datetime(2026, 9, 22, 10, 0, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def quiet_waits(monkeypatch):
    # These tests read a command's output; test_console covers the wait line.
    monkeypatch.setattr(console, "waiting", lambda message: nullcontext(console.Wait(message)))


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


def read(run, since):
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
    assert lines[0].split() == ["#", "Started", "Duration", "Status", "Exit", "Code", "Id"]
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
                                  "exit_code": 0, "number": 1}
    assert runs_cli.parse_runs("preflight line\n" + out) == [
        dataclasses.replace(NEWEST, number=1), dataclasses.replace(OLDER, number=2)]


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


def test_runs_json_shows_the_runs_the_table_shows(capsys):
    assert runs_cli.runs(lambda: hourly_runs(12), "my-report", ["--json"]) == 0
    assert len(json.loads(capsys.readouterr().out)) == runs_cli.DEFAULT_RUNS


def test_runs_span_keeps_the_runs_before_since_plus_span(capsys):
    assert runs_cli.runs(lambda: hourly_runs(12), "my-report",
                         ["--since", "2026-09-23T00:00", "--span", "3h"]) == 0
    ids = [line.split()[-1] for line in capsys.readouterr().out.splitlines()[1:]]
    assert ids == ["run-8", "run-9", "run-10"]


def test_runs_span_needs_since(capsys):
    assert runs_cli.runs(lambda: pytest.fail("listed runs"), "my-report",
                         ["--span", "3h"]) == 1
    assert "--span needs --since" in capsys.readouterr().out


def test_runs_span_rejects_a_date(capsys):
    assert runs_cli.runs(lambda: pytest.fail("listed runs"), "my-report",
                         ["--since", "3d", "--span", "2026-09-20"]) == 1
    assert "12h, 3d, 2w" in capsys.readouterr().out


def test_runs_span_with_no_run_in_the_window(capsys):
    assert runs_cli.runs(lambda: [OLDER], "my-report",
                         ["--since", "2026-09-23T00:00", "--span", "3h"]) == 0
    assert capsys.readouterr().out == ("my-report has not run between 2026-09-23 00:00:00 "
                                       "and 2026-09-23 03:00:00.\n")


def test_runs_count_caps_the_list_and_lifts_the_default_with_since(capsys):
    assert runs_cli.runs(lambda: hourly_runs(12), "my-report", ["--count", "3"]) == 0
    assert len(capsys.readouterr().out.splitlines()) == 1 + 3
    assert runs_cli.runs(lambda: hourly_runs(12), "my-report", ["--since", "2026-09-01"]) == 0
    assert len(capsys.readouterr().out.splitlines()) == 1 + 12
    assert runs_cli.runs(lambda: hourly_runs(12), "my-report",
                         ["--since", "2026-09-01", "--count", "2", "--json"]) == 0
    assert [run["id"] for run in json.loads(capsys.readouterr().out)] == ["run-0", "run-1"]


def test_runs_count_must_be_1_or_more(capsys):
    assert runs_cli.runs(lambda: pytest.fail("listed runs"), "my-report",
                         ["--count", "0"]) == 1
    assert "--count must be 1 or more" in capsys.readouterr().out


def test_runs_since_with_no_run_in_the_window(capsys):
    assert runs_cli.runs(lambda: [OLDER], "my-report", ["--since", "1h"]) == 0
    assert capsys.readouterr().out == "my-report has not run since 1h.\n"


def test_runs_with_a_bad_since_value_names_the_forms(capsys):
    assert runs_cli.runs(lambda: pytest.fail("listed runs"), "my-report",
                         ["--since", "soon"]) == 1
    assert "2026-09-20T14:00" in capsys.readouterr().out


def test_a_window_keeps_the_numbers_of_the_full_list(capsys):
    resolved = []
    assert runs_cli.runs(lambda: hourly_runs(12), "my-report",
                         ["--since", "2026-09-23T02:00", "--span", "3h"],
                         lambda found: resolved.append([run.id for run in found])) == 0
    rows = [line.split() for line in capsys.readouterr().out.splitlines()[1:]]
    assert [(row[0], row[-1]) for row in rows] == [("7", "run-6"), ("8", "run-7"), ("9", "run-8")]
    assert resolved == [["run-6", "run-7", "run-8"]]


def test_logs_n_reads_run_n_of_the_full_list_and_resolves_only_it(capsys):
    resolved = []
    assert runs_cli.logs(lambda: hourly_runs(12), lambda run, since: [], "my-report", ["8"],
                         lambda found: resolved.append([run.id for run in found])) == 0
    assert capsys.readouterr().out.startswith("run 8 of my-report: started 2026-09-23 03:00:12")
    assert resolved == [["run-7"]]


def test_logs_n_ignores_the_window(capsys):
    assert runs_cli.logs(lambda: hourly_runs(12), lambda run, since: [], "my-report",
                         ["12", "--count", "2"]) == 0
    assert capsys.readouterr().out.startswith("run 12 of my-report:")


def test_logs_n_past_the_full_list_names_its_length(capsys):
    assert runs_cli.logs(lambda: hourly_runs(12), lambda run, since: [], "my-report", ["13"]) == 1
    assert ("pdt runs my-report knows 12 runs; pick a number from 1 to 12"
            in capsys.readouterr().out)


def test_logs_without_n_reads_the_newest_run_in_the_window(capsys):
    assert runs_cli.logs(lambda: hourly_runs(12), lambda run, since: [], "my-report",
                         ["--since", "2026-09-23T02:00", "--span", "3h"]) == 0
    assert capsys.readouterr().out.startswith("run 7 of my-report:")


def test_logs_failed_picks_the_newest_failed_run_in_the_window(capsys):
    found = [dataclasses.replace(run, status="failed", exit_code=1) if index in (1, 4) else run
             for index, run in enumerate(hourly_runs(12))]
    resolved = []
    assert runs_cli.logs(lambda: found, lambda run, since: [], "my-report",
                         ["--failed", "--since", "2026-09-23T02:00", "--span", "7h"],
                         lambda runs: resolved.append([run.id for run in runs])) == 1
    assert capsys.readouterr().out.startswith("run 5 of my-report:")
    assert resolved == [[f"run-{hours}" for hours in range(2, 9)]]


def test_runs_with_no_runs(capsys):
    assert runs_cli.runs(list, "my-report", []) == 0
    assert capsys.readouterr().out == ("my-report has not run yet.\n"
                                       "  pdt run my-report --deployed --follow  start one run now and follow its log\n")


def test_logs_shows_the_newest_run_with_its_exit_line(capsys):
    assert runs_cli.logs(list_two, read, "my-report", []) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "run 1 of my-report: started 2026-09-23 10:00:12, 12s, succeeded, exit 0"
    assert lines[1] == "10:00:12 DEBUG   connecting"
    assert lines[3] == "10:00:24 WARNING slow"
    assert lines[4] == "10:00:24         pdt: exit 0"
    assert len(lines) == 5


def test_logs_picks_run_n_and_exits_1_when_it_failed(capsys):
    assert runs_cli.logs(list_two, read, "my-report", ["2"]) == 1
    assert capsys.readouterr().out.startswith("run 2 of my-report:")


def test_logs_failed_picks_the_newest_failed_run(capsys):
    assert runs_cli.logs(list_two, read, "my-report", ["--failed"]) == 1
    assert capsys.readouterr().out.startswith("run 2 of my-report:")


def test_logs_id_reads_that_run_and_resolves_only_it(capsys):
    resolved = []
    assert runs_cli.logs(list_two, read, "my-report", ["--id", "stream-1"],
                         resolve=resolved.extend) == 1
    assert capsys.readouterr().out.startswith("run 2 of my-report:")
    assert [run.id for run in resolved] == ["stream-1"]


def test_logs_several_ids_give_an_object_keyed_by_id(capsys):
    asked = []

    def read_many(runs):
        asked.append([run.id for run in runs])
        return {run.id: LINES[run.id] for run in runs}

    assert runs_cli.logs(list_two, read, "my-report",
                         ["--id", "stream-2", "--id", "stream-1", "--full", "--json"],
                         read_many=read_many) == 1
    out = json.loads(capsys.readouterr().out)
    assert asked == [["stream-2", "stream-1"]]
    assert list(out) == ["stream-2", "stream-1"]
    assert [line["message"] for line in out["stream-1"]] == [
        "boom", "Traceback (most recent)", "done", "pdt: exit 1"]


def test_logs_id_unknown_names_the_id(capsys):
    assert runs_cli.logs(list_two, read, "my-report", ["--id", "stream-9"]) == 1
    assert "knows no run with id stream-9" in capsys.readouterr().out


def test_logs_failed_with_no_failed_run(capsys):
    assert runs_cli.logs(lambda: [NEWEST], read, "my-report", ["--failed"]) == 0
    assert capsys.readouterr().out == "my-report has no failed run in its last 1 runs.\n"


def test_logs_shows_the_last_20_lines_unless_full(capsys):
    lines = [Line(T0, "INFO", f"line {index}") for index in range(25)]
    runs_cli.logs(lambda: [Run("r1", T0, T1, "succeeded")], lambda run, since: lines, "my-report", [])
    out = capsys.readouterr().out
    assert "the last 20 of 25 lines" in out
    assert "line 4" not in out and "line 5" in out and "line 24" in out
    runs_cli.logs(lambda: [Run("r1", T0, T1, "succeeded")], lambda run, since: lines, "my-report",
                  ["--full"])
    out = capsys.readouterr().out
    assert "of 25 lines" not in out and "line 0" in out


def test_logs_lines_and_head_pick_which_lines_print(capsys):
    lines = [Line(T0, "INFO", f"line {index}") for index in range(25)]
    one_run = [Run("r1", T0, T1, "succeeded")]
    runs_cli.logs(lambda: one_run, lambda run, since: lines, "my-report", ["--lines", "3"])
    out = capsys.readouterr().out
    assert "the last 3 of 25 lines; add --full for all of them" in out
    assert "line 21" not in out and "line 22" in out and "line 24" in out
    runs_cli.logs(lambda: one_run, lambda run, since: lines, "my-report", ["--head"])
    out = capsys.readouterr().out
    assert "the first 20 of 25 lines; add --full for all of them" in out
    assert "line 0" in out and "line 19" in out and "line 20" not in out
    runs_cli.logs(lambda: one_run, lambda run, since: lines, "my-report", ["--head", "--lines", "2"])
    out = capsys.readouterr().out
    assert "the first 2 of 25 lines" in out and "line 1\n" in out and "line 2\n" not in out
    runs_cli.logs(lambda: one_run, lambda run, since: lines, "my-report", ["--lines", "30"])
    assert "of 25 lines" not in capsys.readouterr().out


@pytest.mark.parametrize("extra", [["--lines", "5"], ["--head"]])
def test_logs_full_with_lines_or_head_is_an_error(capsys, extra):
    assert runs_cli.logs(lambda: pytest.fail("listed runs"), read, "my-report",
                         ["--full", *extra]) == 1
    assert "--full prints every line; drop --lines and --head" in capsys.readouterr().out


def test_logs_lines_must_be_1_or_more(capsys):
    assert runs_cli.logs(lambda: pytest.fail("listed runs"), read, "my-report",
                         ["--lines", "0"]) == 1
    assert "--lines must be 1 or more" in capsys.readouterr().out


def test_logs_errors_keeps_warnings_errors_and_plain_lines(capsys):
    assert runs_cli.logs(list_two, read, "my-report", ["2", "--errors", "--json"]) == 1
    records = json.loads(capsys.readouterr().out)
    assert [record["message"] for record in records] == [
        "boom", "Traceback (most recent)", "pdt: exit 1"]
    assert records[0] == {"time": EARLIER.isoformat(), "level": "ERROR", "message": "boom"}


def test_logs_with_no_runs(capsys):
    assert runs_cli.logs(list, read, "my-report", ["--failed"]) == 0
    assert capsys.readouterr().out == ("my-report has not run yet.\n"
                                       "  pdt run my-report --deployed --follow  start one run now and follow its log\n")


@pytest.mark.parametrize("list_runs, rest", [
    (list, ["--json"]),
    (list, ["--failed", "--json"]),
    (lambda: [NEWEST], ["--failed", "--json"]),
    (lambda: [OLDER], ["--since", "1h", "--json"]),
])
def test_logs_json_with_no_run_to_read_prints_an_empty_list(capsys, list_runs, rest):
    console.to_stderr()
    assert runs_cli.logs(list_runs, read, "my-report", rest) == 0
    assert capsys.readouterr().out == "[]\n"


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


RUNNING = Run("stream-3", T0, None, "running")


def test_logs_of_a_running_run_with_no_lines_yet_offers_follow(capsys):
    assert runs_cli.logs(lambda: [RUNNING], lambda run, since: [], "report", [],
                         store="Azure Log Analytics", delay=timedelta(minutes=10)) == 0
    out = capsys.readouterr().out
    assert "run 1 is still running, and Azure Log Analytics has no lines from it yet." in out
    assert "pdt logs report 1 --follow" in out


def test_logs_of_a_running_run_says_more_lines_will_come(capsys):
    assert runs_cli.logs(lambda: [RUNNING], lambda run, since: [Line(T0, "INFO", "started")],
                         "report", []) == 0
    out = capsys.readouterr().out
    assert "run 1 is still running, so more lines will come." in out


def test_follow_prints_each_new_line_once_until_the_run_ends(monkeypatch, capsys):
    ended = dataclasses.replace(RUNNING, ended=T1, status="succeeded", exit_code=0)
    states = iter([[RUNNING], [RUNNING], [ended]])
    reads = iter([
        [Line(T0, "INFO", "started")],
        [Line(T0, "INFO", "started"), Line(T0, "INFO", "row"), Line(T0, "INFO", "row")],
        [Line(T0, "INFO", "started"), Line(T0, "INFO", "row"), Line(T0, "INFO", "row"),
         Line(T1, "INFO", "done")],
    ])
    first = next(reads)
    sleeps = []
    monkeypatch.setattr(runs_cli.time, "sleep", sleeps.append)
    assert runs_cli.logs(lambda: next(states),
                         lambda run, since: first if not sleeps else next(reads),
                         "report", ["--follow"]) == 0
    out = capsys.readouterr().out
    assert out.count("INFO    started") == 1
    assert out.count("row") == 2
    assert out.count("done") == 1
    assert out.count("run 1 of report") == 1
    assert out.rstrip().endswith("the run succeeded after 12s, exit 0")
    assert sleeps == [runs_cli.FOLLOW_SECONDS] * 2


def test_follow_with_json_or_head_is_an_error(capsys):
    assert runs_cli.logs(list_two, read, "report", ["--follow", "--json"]) == 1
    assert "--follow prints lines as they arrive" in capsys.readouterr().out


@pytest.mark.parametrize("options", [
    {}, {"store": "CloudWatch Logs", "delay": timedelta(minutes=5), "hold": timedelta(seconds=10)}])
def test_follow_of_an_ended_run_prints_what_logs_prints(options, monkeypatch, capsys):
    monkeypatch.setattr(runs_cli.time, "sleep", lambda seconds: pytest.fail("nothing to wait for"))
    assert runs_cli.logs(list_two, read, "report", [], **options) == 0
    plain = capsys.readouterr().out
    assert runs_cli.logs(list_two, read, "report", ["--follow"], **options) == 0
    assert capsys.readouterr().out == plain


def test_follow_of_a_running_run_says_so_once_and_ends_with_its_result(monkeypatch, capsys):
    started = Line(T0, "INFO", "started")
    reads = iter([[started], [started, Line(T1, "ERROR", "boom"), Line(T1, "", "pdt: exit 7")]])
    monkeypatch.setattr(runs_cli.time, "sleep", lambda seconds: None)
    assert runs_cli.logs(lambda: [RUNNING], lambda run, since: next(reads), "report",
                         ["--follow"]) == 1
    assert capsys.readouterr().out.splitlines() == [
        "run 1 of report: started 2026-09-23 10:00:12",
        "watching for log output for report; press Ctrl+C to stop, the run keeps going",
        "10:00:12 INFO    started",
        "10:00:24 ERROR   boom",
        "10:00:24         pdt: exit 7",
        "the run failed after 12s, exit 7",
    ]


def test_follow_of_an_ended_run_waits_for_the_lines_the_store_has_not_shown(monkeypatch, capsys):
    ended_at = datetime.now(UTC).replace(microsecond=0) - timedelta(seconds=20)
    ended = Run("stream-4", ended_at - timedelta(seconds=8), ended_at, "failed", 7)
    started = Line(ended.started, "INFO", "started")
    reads = iter([[started], [started, Line(ended_at, "ERROR", "boom"),
                              Line(ended_at, "", "pdt: exit 7")]])
    monkeypatch.setattr(runs_cli.time, "sleep", lambda seconds: None)
    assert runs_cli.logs(lambda: [ended], lambda run, since: next(reads), "report", ["--follow"],
                         store="CloudWatch Logs", delay=timedelta(minutes=5)) == 1
    out = capsys.readouterr().out
    until = runs_cli.local_text(ended_at + timedelta(minutes=5), "%H:%M:%S")
    assert out.splitlines()[:2] == [
        f"run 1 of report: started {runs_cli.started_text(ended)}",
        f"the run ended at {runs_cli.local_text(ended_at, '%H:%M:%S')}, and CloudWatch Logs can "
        f"take up to 5 minutes to show its last lines, so pdt waits until {until} at the latest; "
        "press Ctrl+C to stop"]
    assert out.count("failed") == 1
    assert out.rstrip().endswith("pdt: exit 7\nthe run failed after 8s, exit 7")


def live_stream(snapshot, connections):
    """A fake `stream`: the snapshot for follow=False, then one list of lines per connection."""
    opened = iter(connections)

    def stream(run, follow):
        return list(snapshot) if not follow else list(next(opened))
    return stream


def no_store(run, since):
    raise AssertionError("a running run with a live stream never reads the store")


def test_logs_reads_a_running_run_from_its_live_log(capsys):
    stream = live_stream([Line(T0, "INFO", "started"), Line(T1, "INFO", "row 1")], [])
    assert runs_cli.logs(lambda: [RUNNING], no_store, "report", [],
                         store="Azure Log Analytics", stream=stream) == 0
    out = capsys.readouterr().out
    assert "row 1" in out
    assert "run 1 is still running, so more lines will come." in out


def test_logs_of_a_running_run_whose_container_has_written_nothing(capsys):
    assert runs_cli.logs(lambda: [RUNNING], no_store, "report", [],
                         store="Azure Log Analytics", stream=live_stream([], [])) == 0
    assert "run 1 is still running, and has written no lines yet." in capsys.readouterr().out


def test_logs_reads_an_ended_run_from_the_store(capsys):
    def stream(run, follow):
        raise AssertionError("an ended run has no live log")
    assert runs_cli.logs(list_two, read, "report", [], stream=stream) == 0
    assert "slow" in capsys.readouterr().out


def test_follow_prints_a_running_run_from_its_live_log_in_order(monkeypatch, capsys):
    states = iter([[RUNNING], [RUNNING], [RUNNING]])
    first = [Line(T0, "INFO", "started")]
    stream = live_stream(first, [
        [],  # the container has not started yet
        [*first, Line(T0, "INFO", "row"), Line(T0, "INFO", "row"), Line(T1, "INFO", "done"),
         Line(T1, "", "pdt: exit 0")],
    ])
    sleeps = []
    monkeypatch.setattr(runs_cli.time, "sleep", sleeps.append)
    assert runs_cli.logs(lambda: next(states), no_store, "report", ["--follow"],
                         stream=stream) == 0
    out = capsys.readouterr().out
    assert out.count("INFO    started") == 1
    assert out.count("row") == 2
    assert out.index("INFO    started") < out.index("row") < out.index("done")
    # The provider still shows the run as running, so the exit line gives its end.
    assert out.rstrip().endswith("pdt: exit 0\nthe run succeeded after 12s, exit 0")
    # One wait for the container to start; none for the provider to record the end.
    assert sleeps == [runs_cli.FOLLOW_SECONDS]


def test_follow_reads_the_store_when_the_live_log_fails(monkeypatch, capsys):
    ended = dataclasses.replace(RUNNING, ended=T1, status="succeeded", exit_code=0)
    states = iter([[RUNNING], [ended]])

    def stream(run, follow):
        if follow:
            yield Line(T0, "INFO", "row 1")
            raise runs_cli.StreamError("connection reset")
        yield Line(T0, "INFO", "started")

    store = [Line(T0, "INFO", "started"), Line(T0, "INFO", "row 1"), Line(T1, "INFO", "row 2"),
             Line(T1, "", "pdt: exit 0")]
    monkeypatch.setattr(runs_cli.time, "sleep", lambda seconds: None)
    assert runs_cli.logs(lambda: next(states), lambda run, since: store, "report", ["--follow"],
                         store="Azure Log Analytics", stream=stream) == 0
    out = capsys.readouterr().out
    assert "pdt cannot read the run's live log, so it reads Azure Log Analytics" in out
    assert out.count("row 1") == 1
    assert out.count("row 2") == 1


class Clock(datetime):
    moments: list[datetime] = []

    @classmethod
    def now(cls, tz=None):
        return cls.moments.pop(0)


@pytest.mark.parametrize("hold", [timedelta(seconds=30), timedelta(seconds=10)])
def test_follow_holds_a_store_line_until_it_is_old_enough_to_print_in_order(hold, monkeypatch,
                                                                            capsys):
    late = Line(T0 + timedelta(seconds=4), "INFO", "late")
    newer = Line(T0 + timedelta(seconds=6), "INFO", "newer")
    done = Line(T0 + timedelta(seconds=8), "INFO", "done")
    exit_line = Line(T0 + timedelta(seconds=8), "", "pdt: exit 0")
    # Without the hold, "newer" would print at the first poll and "late" after it.
    reads = iter([[], [newer], [late, newer], *[[late, newer, done, exit_line]] * 2])
    ended = dataclasses.replace(RUNNING, ended=T0 + timedelta(seconds=9), status="succeeded")
    states = iter([[RUNNING], [RUNNING], [ended]])
    Clock.moments = [T0, T0 + hold, T0 + hold + timedelta(seconds=5),
                     T0 + hold + timedelta(seconds=10)]
    monkeypatch.setattr(runs_cli, "datetime", Clock)
    monkeypatch.setattr(runs_cli.time, "sleep", lambda seconds: None)
    assert runs_cli.follow(lambda: next(states), lambda run, since: next(reads), None, RUNNING,
                           False, timedelta(minutes=5), [], hold=hold) == 0
    out = capsys.readouterr().out
    assert out.index("late") < out.index("newer") < out.index("done")


def test_follow_stops_at_the_exit_line_without_waiting_for_the_store_delay(monkeypatch, capsys):
    hold = timedelta(seconds=30)
    written = datetime.now(UTC) - hold
    first = [Line(T0, "INFO", "started")]
    after = [*first, Line(written, "ERROR", "boom"), Line(written, "", "pdt: exit 2")]
    sleeps = []
    monkeypatch.setattr(runs_cli.time, "sleep", sleeps.append)
    # The provider still shows the run as running; the exit line ends it.
    assert runs_cli.logs(lambda: [RUNNING], lambda run, since: after if sleeps else first, "report",
                         ["--follow"], delay=timedelta(minutes=5), hold=hold) == 1
    out = capsys.readouterr().out
    assert "boom" in out
    assert out.splitlines()[-1].startswith("the run failed after ")
    assert out.rstrip().endswith(", exit 2")
    assert sleeps == [runs_cli.FOLLOW_SECONDS] * 2


@pytest.mark.parametrize("hold", [timedelta(seconds=30), timedelta(seconds=10)])
def test_follow_waits_for_late_lines_after_a_new_exit_line(hold, monkeypatch, capsys):
    now = datetime.now(UTC)
    first = [Line(T0, "INFO", "started")]
    late = Line(now - timedelta(seconds=1), "INFO", "late")
    exit_line = Line(now, "", "pdt: exit 0")
    reads = iter([first, [*first, exit_line], *[[*first, late, exit_line]] * 2])
    Clock.moments = [now, now + timedelta(seconds=5), now + hold]
    monkeypatch.setattr(runs_cli, "datetime", Clock)
    monkeypatch.setattr(runs_cli.time, "sleep", lambda seconds: None)
    assert runs_cli.follow(lambda: [RUNNING], lambda run, since: next(reads), None, RUNNING,
                           False, timedelta(minutes=5), hold=hold, shown=first) == 0
    assert "late" in capsys.readouterr().out


def test_an_ended_run_with_its_exit_line_needs_no_wait_note(capsys):
    ended = Run("stream-4", datetime.now(UTC), datetime.now(UTC), "succeeded", 0)
    lines = [Line(ended.started, "INFO", "done"), Line(ended.started, "", "pdt: exit 0")]
    assert runs_cli.logs(lambda: [ended], lambda run, since: lines, "report", [],
                         store="CloudWatch Logs", delay=timedelta(minutes=5)) == 0
    assert "can take up to" not in capsys.readouterr().out


def test_an_ended_run_without_its_exit_line_says_the_last_lines_may_be_missing(capsys):
    ended = Run("stream-4", datetime.now(UTC), datetime.now(UTC), "succeeded", 0)
    assert runs_cli.logs(lambda: [ended], lambda run, since: [Line(ended.started, "INFO", "row")],
                         "report", [], store="CloudWatch Logs", delay=timedelta(minutes=5)) == 0
    assert ("CloudWatch Logs can take up to 5 minutes to show a line, so the last lines may "
            "not be there yet.") in capsys.readouterr().out


def test_follow_prints_a_line_written_after_logs_read_the_store(monkeypatch, capsys):
    hold = timedelta(seconds=30)
    written = datetime.now(UTC) - hold
    ended = dataclasses.replace(RUNNING, ended=written, status="succeeded", exit_code=0)
    first = [Line(T0, "INFO", "started")]
    reads = iter([first, *[[*first, Line(written, "INFO", "between"),
                            Line(written, "", "pdt: exit 0")]] * 2])
    sleeps = []
    monkeypatch.setattr(runs_cli.time, "sleep", sleeps.append)
    assert runs_cli.logs(lambda: [ended], lambda run, since: next(reads), "report", ["--follow"],
                         delay=timedelta(minutes=5), hold=hold) == 0
    assert "between" in capsys.readouterr().out
    assert sleeps == []


def test_follow_with_no_lines_shown_prints_every_line_of_the_live_log(monkeypatch, capsys):
    ended = dataclasses.replace(RUNNING, ended=T1, status="succeeded", exit_code=0)
    stream = live_stream([Line(T0, "INFO", "started")],
                         [[Line(T0, "INFO", "started"), Line(T1, "INFO", "done")]])
    monkeypatch.setattr(runs_cli.time, "sleep", lambda seconds: None)
    assert runs_cli.follow(lambda: [ended], no_store, None, RUNNING, False,
                           timedelta(minutes=5), stream=stream, shown=[]) == 0
    out = capsys.readouterr().out
    assert out.index("INFO    started") < out.index("done")


def test_follow_reads_the_store_for_a_run_that_ended_before_its_live_log_showed_a_line(
        monkeypatch, capsys):
    ended = dataclasses.replace(RUNNING, ended=T1, status="succeeded", exit_code=0)
    lines = [Line(T0, "INFO", "started"), Line(T1, "", "pdt: exit 0")]
    monkeypatch.setattr(runs_cli.time, "sleep", lambda seconds: None)
    assert runs_cli.follow(lambda: [ended], lambda run, since: lines, None, RUNNING, False,
                           timedelta(minutes=5), [], stream=live_stream([], [[]]),
                           store="Azure Log Analytics") == 0
    out = capsys.readouterr().out
    assert [out.count(text) for text in ("INFO    started", "pdt: exit 0")] == [1, 1]
    assert out.rstrip().endswith("the run succeeded after 12s, exit 0")


def test_follow_reads_the_live_log_once_more_for_a_run_that_ended_between_two_looks(
        monkeypatch, capsys):
    ended = dataclasses.replace(RUNNING, ended=T1, status="succeeded", exit_code=0)
    looks = iter([[], [Line(T0, "INFO", "started"), Line(T1, "", "pdt: exit 0")]])
    monkeypatch.setattr(runs_cli.time, "sleep", lambda seconds: None)
    assert runs_cli.follow(lambda: [ended], no_store, None, RUNNING, False,
                           timedelta(minutes=5), [], stream=lambda run, follow: next(looks)) == 0
    out = capsys.readouterr().out
    assert [out.count(text) for text in ("INFO    started", "pdt: exit 0")] == [1, 1]


def test_follow_ends_at_an_exit_line_the_live_log_showed_before_it_started(monkeypatch, capsys):
    stream = live_stream([Line(T0, "INFO", "started"), Line(T1, "", "pdt: exit 3")], [])
    sleeps = []
    monkeypatch.setattr(runs_cli.time, "sleep", sleeps.append)
    assert runs_cli.logs(lambda: [RUNNING], no_store, "report", ["--follow"],
                         stream=stream) == 1
    out = capsys.readouterr().out
    assert out.splitlines()[0].endswith("12s, failed, exit 3")
    assert "following" not in out
    assert sleeps == []


def store_reads(batches):
    """A fake store read: one batch per call, and the `since` of each call."""
    asked = []
    found = iter(batches)

    def read(since):
        asked.append(since)
        return next(found)
    return read, asked


def test_store_and_tail_gives_the_store_then_each_new_tail_line(monkeypatch):
    hold = timedelta(seconds=30)
    started, row, late = Line(T0, "INFO", "started"), Line(T1, "INFO", "row"), Line(T0, "INFO", "late")
    read, asked = store_reads([[started], [started, late]])
    Clock.moments = [T0, T0, T0 + timedelta(seconds=1), T0 + timedelta(seconds=2)]
    monkeypatch.setattr(runs_cli, "datetime", Clock)
    # The tail repeats a line the store gave; the job wrote "row" twice.
    lines = list(runs_cli.store_and_tail(read, [started, row, row], hold))
    assert lines == [started, row, row, late]
    # The tail ended before `hold` passed, so the store is read again from before it opened.
    assert asked == [None, T0 - hold]


def test_store_and_tail_reads_the_store_again_once_hold_has_passed(monkeypatch):
    hold = timedelta(seconds=10)
    started, late = Line(T0, "INFO", "started"), Line(T0, "INFO", "late")
    first, second = Line(T0, "INFO", "first"), Line(T1, "INFO", "second")
    read, asked = store_reads([[started], [started, late, first]])
    Clock.moments = [T0, T0 + timedelta(seconds=5), T0 + hold]
    monkeypatch.setattr(runs_cli, "datetime", Clock)
    lines = list(runs_cli.store_and_tail(read, [first, second, Line(T1, "INFO", "third")], hold))
    assert lines == [started, first, late, second, Line(T1, "INFO", "third")]
    assert asked == [None, T0 - hold]


def test_store_and_tail_ends_at_the_exit_line_after_one_more_store_read(monkeypatch):
    hold = timedelta(seconds=30)
    started, exit_line = Line(T0, "INFO", "started"), Line(T1, "", "pdt: exit 0")
    read, asked = store_reads([[started], [started]])

    def tail():
        yield exit_line
        raise AssertionError("the tail is not read past the exit line")
    Clock.moments = [T0]
    monkeypatch.setattr(runs_cli, "datetime", Clock)
    assert list(runs_cli.store_and_tail(read, tail(), hold)) == [started, exit_line]
    assert asked == [None, T0 - hold]


def test_store_and_tail_with_the_exit_line_in_the_store_never_reads_the_tail(monkeypatch):
    exit_line = Line(T1, "", "pdt: exit 0")
    read, asked = store_reads([[Line(T0, "INFO", "started"), exit_line]])

    def tail():
        raise AssertionError("the store already holds the exit line")
        yield
    lines = list(runs_cli.store_and_tail(read, tail(), timedelta(seconds=30)))
    assert lines[-1] == exit_line
    assert asked == [None]


def test_follow_prints_each_line_of_the_store_and_the_tail_once(monkeypatch, capsys):
    store = [Line(T0, "INFO", "started"), Line(T0, "INFO", "row")]
    tail = [Line(T0, "INFO", "row"), Line(T1, "INFO", "done"), Line(T1, "", "pdt: exit 0")]

    def stream(run, follow):
        if not follow:
            return list(store)
        return runs_cli.store_and_tail(lambda since: store, tail, timedelta(0))
    monkeypatch.setattr(runs_cli.time, "sleep", lambda seconds: None)
    assert runs_cli.logs(lambda: [RUNNING], no_store, "report", ["--follow"], stream=stream) == 0
    out = capsys.readouterr().out
    assert [out.count(text) for text in ("INFO    started", "row", "done", "pdt: exit 0")] == [1, 1, 1, 1]
    assert out.rstrip().endswith("the run succeeded after 12s, exit 0")


def test_follow_holds_the_newest_lines_of_its_first_read_of_the_store(monkeypatch, capsys):
    now = T0 + timedelta(hours=1)
    started = Line(T0, "INFO", "started")
    late = Line(now - timedelta(seconds=5), "INFO", "late")
    newer = Line(now - timedelta(seconds=2), "INFO", "newer")
    exit_line = Line(now - timedelta(seconds=1), "", "pdt: exit 0")
    reads = [[started, newer], [started, late, newer, exit_line]]
    Clock.moments = [now, now, now + timedelta(seconds=10)]
    monkeypatch.setattr(runs_cli, "datetime", Clock)
    monkeypatch.setattr(runs_cli.time, "sleep", lambda seconds: None)
    assert runs_cli.logs(lambda: [RUNNING], lambda run, since: reads.pop(0) if len(reads) > 1 else reads[0],
                         "report", ["--follow"], hold=timedelta(seconds=10)) == 0
    out = capsys.readouterr().out
    assert out.index("started") < out.index("late") < out.index("newer")


def test_follow_prints_a_line_the_store_shows_later_than_the_hold(monkeypatch, capsys):
    now = T0 + timedelta(hours=1)
    first = [Line(T0, "INFO", "started")]
    exit_line = Line(now - timedelta(seconds=11), "", "pdt: exit 0")
    late = Line(now - timedelta(seconds=12), "INFO", "late")
    reads = [first, [*first, exit_line], [*first, late, exit_line]]
    Clock.moments = [now, now]
    monkeypatch.setattr(runs_cli, "datetime", Clock)
    monkeypatch.setattr(runs_cli.time, "sleep", lambda seconds: None)
    assert runs_cli.follow(lambda: [RUNNING], lambda run, since: reads.pop(0), None, RUNNING,
                           False, timedelta(minutes=5), hold=timedelta(seconds=10),
                           shown=first) == 0
    assert "late" in capsys.readouterr().out


def test_follow_of_a_run_whose_exit_line_is_old_reads_the_store_once(monkeypatch, capsys):
    now = T0 + timedelta(hours=1)
    reads = [[Line(T0, "INFO", "started"), Line(T1, "", "pdt: exit 0")]]
    sleeps = []
    Clock.moments = [now]
    monkeypatch.setattr(runs_cli, "datetime", Clock)
    monkeypatch.setattr(runs_cli.time, "sleep", sleeps.append)
    assert runs_cli.follow(lambda: [RUNNING], lambda run, since: reads.pop(0), None, RUNNING,
                           False, timedelta(minutes=5), hold=timedelta(seconds=10), shown=[]) == 0
    assert "started" in capsys.readouterr().out
    assert sleeps == []


def test_follow_reads_the_store_from_the_newest_line_printed_minus_the_hold(monkeypatch, capsys):
    hold = timedelta(seconds=10)
    now = T0 + timedelta(hours=1)
    first = [Line(T0, "INFO", "started")]
    late = Line(now - timedelta(seconds=20), "INFO", "late")
    newer = Line(now - timedelta(seconds=2), "INFO", "newer")
    exit_line = Line(now + timedelta(seconds=1), "", "pdt: exit 0")
    shows = [[*first, newer], [*first, late, newer], [*first, late, newer, exit_line]]
    asked = []

    def read_lines(run, since):
        asked.append(since)
        lines = shows.pop(0) if len(shows) > 1 else shows[0]
        return [line for line in lines if since is None or line.time >= since]

    Clock.moments = [now, now + hold, now + 2 * hold]
    monkeypatch.setattr(runs_cli, "datetime", Clock)
    monkeypatch.setattr(runs_cli.time, "sleep", lambda seconds: None)
    assert runs_cli.follow(lambda: [RUNNING], read_lines, None, RUNNING, False,
                           timedelta(minutes=5), first, hold=hold) == 0
    out = capsys.readouterr().out
    assert out.index("late") < out.index("newer")
    assert out.count("newer") == 1
    assert asked == [None, T0 - hold, newer.time - hold, exit_line.time - hold]


def test_follow_opens_a_quiet_live_log_again_at_once(monkeypatch, capsys):
    clock = iter([0, runs_cli.FOLLOW_SECONDS * 6, 100])
    monkeypatch.setattr(runs_cli.time, "monotonic", lambda: next(clock))
    sleeps = []
    monkeypatch.setattr(runs_cli.time, "sleep", sleeps.append)
    stream = live_stream([], [[Line(T0, "INFO", "started")],
                              [Line(T0, "INFO", "started"), Line(T1, "", "pdt: exit 0")]])
    assert runs_cli.follow(lambda: [RUNNING], no_store, None, RUNNING, False,
                           timedelta(minutes=5), [], stream=stream) == 0
    assert sleeps == []


def test_follow_of_a_recently_ended_run_with_its_exit_line_prints_what_logs_prints(monkeypatch,
                                                                                   capsys):
    ended_at = datetime.now(UTC).replace(microsecond=0) - timedelta(seconds=60)
    ended = Run("stream-4", ended_at - timedelta(seconds=8), ended_at, "failed", 7)
    lines = [Line(ended.started, "INFO", "started"), Line(ended_at, "", "pdt: exit 7")]
    monkeypatch.setattr(runs_cli.time, "sleep", lambda seconds: pytest.fail("nothing to wait for"))
    options = {"store": "CloudWatch Logs", "delay": runs_cli.LOG_DELAY,
               "hold": timedelta(seconds=10)}
    assert runs_cli.logs(lambda: [ended], lambda run, since: lines, "report", [], **options) == 1
    plain = capsys.readouterr().out
    assert runs_cli.logs(lambda: [ended], lambda run, since: lines, "report", ["--follow"],
                         **options) == 1
    assert capsys.readouterr().out == plain


def test_follow_ends_with_the_exit_code_the_store_shows_late(monkeypatch, capsys):
    ended_at = datetime.now(UTC).replace(microsecond=0) - timedelta(seconds=20)
    ended = Run("stream-4", ended_at - timedelta(seconds=8), ended_at, "failed")
    started = Line(ended.started, "INFO", "started")
    reads = iter([[started], [started, Line(ended_at, "", "pdt: exit 7")]])
    monkeypatch.setattr(runs_cli.time, "sleep", lambda seconds: None)
    assert runs_cli.logs(lambda: [ended], lambda run, since: next(reads), "report", ["--follow"],
                         store="Azure Log Analytics", delay=runs_cli.LOG_DELAY) == 1
    out = capsys.readouterr().out
    assert out.splitlines()[0] == f"run 1 of report: started {runs_cli.started_text(ended)}"
    assert out.rstrip().endswith("the run failed after 8s, exit 7")


def test_follow_of_a_run_stopped_without_its_exit_line_says_so(monkeypatch, capsys):
    now = T0 + timedelta(hours=1)
    ended = Run("stream-4", now - timedelta(seconds=28), now - timedelta(seconds=20), "failed")
    monkeypatch.setattr(runs_cli, "datetime", Clock)
    Clock.moments = [now, now, now + runs_cli.LOG_DELAY]
    monkeypatch.setattr(runs_cli.time, "sleep", lambda seconds: None)
    assert runs_cli.logs(lambda: [ended], lambda run, since: [Line(ended.started, "INFO", "started")],
                         "report", ["--follow"], store="Azure Log Analytics",
                         delay=runs_cli.LOG_DELAY) == 1
    assert capsys.readouterr().out.splitlines()[-2:] == [
        "note: Azure Log Analytics does not show the run's exit line, so its log may end early.",
        "the run failed after 8s"]


@pytest.mark.parametrize("rest", [[], ["--errors"]])
def test_logs_of_a_live_run_that_has_just_ended_ends_with_its_exit_line(rest, capsys):
    snapshot = [Line(T0, "INFO", "started"), Line(T1, "", "pdt: exit 0")]
    stream = live_stream(snapshot, [])
    options = {"store": "Azure Log Analytics", "delay": runs_cli.LOG_DELAY, "stream": stream}
    assert runs_cli.logs(lambda: [RUNNING], no_store, "report", rest, **options) == 0
    plain = capsys.readouterr().out
    assert plain.splitlines()[-1] == "10:00:24         pdt: exit 0"
    assert runs_cli.logs(lambda: [RUNNING], no_store, "report", [*rest, "--follow"],
                         **options) == 0
    assert capsys.readouterr().out == plain


def test_logs_errors_of_a_run_with_only_info_lines_says_so(capsys):
    assert runs_cli.logs(list_two, lambda run, since: [Line(T0, "INFO", "row")], "report",
                         ["--errors"], store="CloudWatch Logs") == 0
    assert capsys.readouterr().out.splitlines()[-1] == (
        "note: CloudWatch Logs has no warning or error lines from run 1.")


def test_follow_of_a_new_run_waits_for_the_list_to_show_it_and_never_follows_an_old_one(
        monkeypatch, capsys):
    new = dataclasses.replace(RUNNING, id="new")
    lists = iter([[OLDER], [OLDER], [new, OLDER], [new, OLDER]])
    stream = live_stream([Line(T0, "INFO", "Hello, world!")],
                         [[Line(T0, "INFO", "Hello, world!"), Line(T1, "", "pdt: exit 0")]])
    sleeps = []
    monkeypatch.setattr(runs_cli.time, "sleep", sleeps.append)
    assert runs_cli.logs(lambda: next(lists), no_store, "report", ["--follow"],
                         store="Azure Log Analytics", stream=stream,
                         new=lambda run: run.id == "new") == 0
    assert capsys.readouterr().out.splitlines() == [
        "run 1 of report: started 2026-09-23 10:00:12",
        "watching for log output for report; press Ctrl+C to stop, the run keeps going",
        "10:00:12 INFO    Hello, world!",
        "10:00:24         pdt: exit 0",
        "the run succeeded after 12s, exit 0",
    ]
    assert sleeps == [runs_cli.FOLLOW_SECONDS] * 2


def test_a_new_run_the_list_never_shows_ends_with_the_command_to_follow_it(monkeypatch, capsys):
    listed = []
    sleeps = []
    monkeypatch.setattr(runs_cli.time, "sleep", sleeps.append)
    assert runs_cli.logs(lambda: listed.append(1) or [OLDER], no_store, "report", ["--follow"],
                         new=lambda run: False) == 0
    assert len(listed) == runs_cli.NEW_RUN_TRIES
    assert sleeps == [runs_cli.FOLLOW_SECONDS] * (runs_cli.NEW_RUN_TRIES - 1)
    out = capsys.readouterr().out
    assert "the run list does not show the new run yet" in out
    assert "pdt logs report --follow" in out


PLACE = runs_cli.LogPlace("Log group", "/pdt/my-report", "Stream",
                          lambda runs: [f"stream/{run.id}" for run in runs],
                          lambda: "https://console.example/group", lambda item: f"https://console.example/{item}")


@pytest.fixture
def opened(monkeypatch):
    pages = []
    monkeypatch.setattr(runs_cli.webbrowser, "open", lambda url: pages.append(url) or True)
    return pages


def test_logs_open_without_a_run_opens_the_app_logs_page(opened, capsys):
    assert runs_cli.logs(lambda: pytest.fail("listed runs"), read, "my-report", ["--open"],
                         place=PLACE) == 0
    assert opened == ["https://console.example/group"]
    assert "opened https://console.example/group" in capsys.readouterr().out


def test_logs_open_with_a_run_opens_that_run_log(opened):
    assert runs_cli.logs(list_two, read, "my-report", ["2", "--open"], place=PLACE) == 0
    assert opened == ["https://console.example/stream/stream-1"]


def test_runs_open_opens_the_runs_page_and_says_where_to_look(opened, capsys):
    assert runs_cli.runs(lambda: pytest.fail("listed runs"), "my-report", ["--open"],
                         page="https://console.example/runs", note="look for pdt-my-report") == 0
    assert opened == ["https://console.example/runs"]
    assert "look for pdt-my-report" in capsys.readouterr().out


def test_open_a_missing_folder_says_run_the_app_first(tmp_path, capsys):
    assert runs_cli.open_place(str(tmp_path / "logs")) == 1
    assert "does not exist yet" in capsys.readouterr().out


def test_logs_list_shows_the_exit_code_of_a_running_run_that_has_one(capsys):
    running = Run("stream-3", T1, None, "running", 0)
    assert runs_cli.logs(lambda: [running], read, "my-report", ["--list"], place=PLACE) == 0
    assert capsys.readouterr().out.splitlines()[-1].split()[-1] == "0"


@pytest.mark.parametrize("command", [
    lambda rest: runs_cli.runs(list_two, "my-report", rest, page="https://console.example/runs"),
    lambda rest: runs_cli.logs(list_two, read, "my-report", rest, place=PLACE)])
def test_open_with_json_is_an_error(command, opened, capsys):
    assert command(["--open", "--json"]) == 1
    assert opened == []
    assert "--open does not print JSON; leave out --json" in capsys.readouterr().out


def test_open_without_a_browser_prints_the_page_and_the_note(monkeypatch, capsys):
    monkeypatch.setattr(runs_cli.webbrowser, "open", lambda url: False)
    assert runs_cli.open_place("https://console.example/runs", "look for pdt-my-report") == 0
    out = capsys.readouterr().out
    assert "https://console.example/runs" in out
    assert "look for pdt-my-report" in out
