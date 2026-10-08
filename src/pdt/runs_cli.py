"""CLI helpers behind `pdt runs`, `pdt logs`, and `pdt health`.

Every provider gives this module two callables, and a third when it needs one:
`list_runs()` returns every run the provider keeps, newest first,
`read_lines(run)` returns one run's log lines, oldest first, and
`resolve(runs)` fills status and exit code on the runs about to print or read,
for a provider whose list does not already know them.

A provider whose log store shows a line some time after the job writes it
names the store and that delay, so `pdt logs` can say when a run's lines are
not there yet. `--follow` reads the same store again every FOLLOW_SECONDS
until the run has ended and the delay has passed.

A run's number is its place in the full list, starting at 1. A window
(`--since`, `--span`, `--count`) picks which runs print and never renumbers.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import re
import time
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Callable

from pdt import console

DEFAULT_RUNS = 10
TAIL_LINES = 20
FOLLOW_SECONDS = 10
# How long a cloud provider's log store can take to show a line, the same on every cloud.
# Azure Monitor lists resource logs as "usually available within 3 to 10 minutes"
# (https://learn.microsoft.com/en-us/azure/azure-monitor/logs/data-ingestion-time);
# AWS and Google document no maximum. The windows provider reads a local file, with no delay.
LOG_DELAY = timedelta(minutes=5)
SINCE_UNITS = {"h": "hours", "d": "days", "w": "weeks"}
SINCE_FORMS = ("a count with a unit (12h, 3d, 2w), a date (2026-09-20), "
               "or a date and time (2026-09-20T14:00)")
EXIT_MARKER = "pdt: exit "
LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
TEXT_LINE = re.compile(r"^\d\d:\d\d:\d\d (DEBUG|INFO|WARNING|ERROR)\s+(.*)$")
# Meltano's structlog lines: `2026-09-23T13:00:35.009777Z [warning  ] dbt   message`.
MELTANO_LINE = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z) "
                          r"\[(debug|info|warning|error|critical)\s*\] (.*)$")
MELTANO_LEVELS = {"debug": "DEBUG", "info": "INFO", "warning": "WARNING",
                  "error": "ERROR", "critical": "ERROR"}


@dataclasses.dataclass
class Run:
    id: str
    started: datetime
    ended: datetime | None
    status: str
    exit_code: int | None = None
    number: int = 0


@dataclasses.dataclass
class Line:
    time: datetime | None
    level: str
    message: str


def parse_line(raw: str, time: datetime | None) -> Line:
    """One raw log line, in either format `pdt.utils.log` writes, or as plain text."""
    raw = raw.rstrip()
    if raw.startswith("{"):
        try:
            record = json.loads(raw)
        except ValueError:
            record = None
        if isinstance(record, dict) and "message" in record:
            return json_line(record, time)
    match = TEXT_LINE.match(raw)
    if match:
        return Line(time, match.group(1), match.group(2))
    match = MELTANO_LINE.match(raw)
    if match:
        stamp = datetime.fromisoformat(match.group(1))
        return Line(stamp, MELTANO_LEVELS[match.group(2)], match.group(3))
    return Line(time, "", raw)


def in_time_order(lines: list[Line]) -> list[Line]:
    """Lines by their own times; a line with no time keeps its place after the one before it."""
    keyed = []
    last = None
    for index, line in enumerate(lines):
        if line.time is not None:
            last = line.time
        keyed.append((last or datetime.min.replace(tzinfo=UTC), index, line))
    keyed.sort(key=lambda item: item[:2])
    return [line for _, _, line in keyed]


def json_line(record: dict, time: datetime | None) -> Line:
    """A `pdt.utils.log` json record, with its extra keys shown as the text form shows them."""
    level = str(record.get("severity", "")).upper()
    extra = " ".join(f"{key}={value}" for key, value in record.items()
                     if key not in ("severity", "message"))
    message = str(record["message"]) + (f"  {extra}" if extra else "")
    return Line(time, level if level in LEVELS else "", message)


def exit_code(lines: list[Line]) -> int | None:
    for line in reversed(lines):
        if line.message.startswith(EXIT_MARKER):
            try:
                return int(line.message.removeprefix(EXIT_MARKER))
            except ValueError:
                return None
    return None


def marker_status(lines: list[Line], running: Callable[[], bool]) -> str:
    """The status of a run whose log ends with EXIT_MARKER once it has finished."""
    code = exit_code(lines)
    if code is not None:
        return "succeeded" if code == 0 else "failed"
    return "running" if running() else "failed"


def run_json(run: Run) -> dict:
    return {"id": run.id, "started": run.started.isoformat(),
            "ended": run.ended.isoformat() if run.ended else None, "status": run.status,
            "exit_code": run.exit_code, "number": run.number}


def parse_runs(output: str) -> list[Run] | None:
    """The runs in the last line of `runs --json` output, or None when it holds none."""
    lines = output.strip().splitlines()
    try:
        records = json.loads(lines[-1]) if lines else None
    except ValueError:
        return None
    if not isinstance(records, list):
        return None
    return [Run(record["id"], datetime.fromisoformat(record["started"]),
                datetime.fromisoformat(record["ended"]) if record["ended"] else None,
                record["status"], record["exit_code"], record["number"]) for record in records]


def local_text(moment: datetime, form: str = "%Y-%m-%d %H:%M:%S") -> str:
    """A UTC moment in the user's local time zone."""
    return f"{moment.astimezone():{form}}"


def started_text(run: Run) -> str:
    return local_text(run.started)


def duration_text(run: Run) -> str:
    if run.ended is None:
        return "-"
    seconds = max(0, round((run.ended - run.started).total_seconds()))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60:02d}s"
    return f"{seconds // 3600}h {seconds % 3600 // 60:02d}m"


def parse_amount(value: str) -> timedelta | None:
    """The length of time a count with a unit (12h, 3d, 2w) names, or None."""
    match = re.fullmatch(r"(\d+)([hdw])", value)
    if match is None:
        return None
    return timedelta(**{SINCE_UNITS[match.group(2)]: int(match.group(1))})


def parse_since(value: str, now: datetime) -> datetime:
    """The moment `--since VALUE` names; a date without a time is local midnight."""
    amount = parse_amount(value)
    if amount is not None:
        return now - amount
    for form in ("%Y-%m-%d", "%Y-%m-%dT%H:%M"):
        try:
            return datetime.strptime(value, form).astimezone()
        except ValueError:
            pass
    raise ValueError(f"--since {value} is not a time pdt reads; use {SINCE_FORMS}")


def window(found: list[Run], since: datetime | None, span: timedelta | None,
           count: int | None) -> list[Run]:
    """The runs started in `[since, since + span)`, at most `count`, with their numbers kept."""
    if since is not None:
        found = [run for run in found if run.started >= since
                 and (span is None or run.started < since + span)]
    return found if count is None else found[:count]


def add_window_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--since")
    parser.add_argument("--span")
    parser.add_argument("--count", type=int)


def parse_window(args: argparse.Namespace,
                 now: datetime) -> tuple[datetime | None, timedelta | None, int | None]:
    """The `window` arguments; the DEFAULT_RUNS newest when no `--since` is given."""
    if args.span is not None and args.since is None:
        raise ValueError("--span needs --since")
    if args.count is not None and args.count < 1:
        raise ValueError("--count must be 1 or more")
    since = None if args.since is None else parse_since(args.since, now)
    span = None if args.span is None else parse_amount(args.span)
    if args.span is not None and span is None:
        raise ValueError(f"--span {args.span} is not a length of time pdt reads; "
                         "use a count with a unit (12h, 3d, 2w)")
    if args.count is None and since is None:
        return since, span, DEFAULT_RUNS
    return since, span, args.count


def say_not_run(app_name: str, args: argparse.Namespace, since: datetime | None,
                span: timedelta | None) -> None:
    if since is None:
        console.say(f"{app_name} has not run yet.")
    elif span is None:
        console.say(f"{app_name} has not run since {args.since}.")
    else:
        console.say(f"{app_name} has not run between {local_text(since)} "
                    f"and {local_text(since + span)}.")


def numbered(list_runs: Callable[[], list[Run]]) -> list[Run]:
    return [dataclasses.replace(run, number=number) for number, run in enumerate(list_runs(), 1)]


def runs(list_runs: Callable[[], list[Run]], app_name: str, rest: list[str],
         resolve: Callable[[list[Run]], None] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=f"pdt runs {app_name}")
    add_window_arguments(parser)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(rest)
    try:
        since, span, count = parse_window(args, datetime.now(UTC))
    except ValueError as exc:
        console.error(str(exc))
        return 1
    found = window(numbered(list_runs), since, span, count)
    if resolve is not None:
        resolve(found)
    if args.json:
        console.say(json.dumps([run_json(run) for run in found]))
        return 0
    if not found:
        say_not_run(app_name, args, since, span)
        return 0
    rows = [[str(run.number), started_text(run), duration_text(run), run.status,
             "-" if run.exit_code is None else str(run.exit_code), run.id] for run in found]
    row_styles = [["", "", "", console.RUN_STATUS_COLOURS[run.status], "", "dim"]
                  for run in found]
    console.table(["#", "Started", "Duration", "Status", "Exit Code", "Id"], rows,
                  row_styles=row_styles)
    return 0


def logs(list_runs: Callable[[], list[Run]], read_lines: Callable[[Run], list[Line]],
         app_name: str, rest: list[str],
         resolve: Callable[[list[Run]], None] | None = None,
         store: str = "the log", delay: timedelta = timedelta(0)) -> int:
    parser = argparse.ArgumentParser(prog=f"pdt logs {app_name}")
    parser.add_argument("number", nargs="?", type=int)
    parser.add_argument("--failed", action="store_true")
    parser.add_argument("--errors", action="store_true")
    parser.add_argument("--lines", type=int)
    parser.add_argument("--head", action="store_true")
    parser.add_argument("--full", action="store_true")
    parser.add_argument("--follow", action="store_true")
    add_window_arguments(parser)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(rest)
    if args.full and (args.lines is not None or args.head):
        console.error("--full prints every line; drop --lines and --head")
        return 1
    if args.follow and (args.json or args.head):
        console.error("--follow prints lines as they arrive; drop --json and --head")
        return 1
    if args.lines is not None and args.lines < 1:
        console.error("--lines must be 1 or more")
        return 1
    try:
        since, span, count = parse_window(args, datetime.now(UTC))
    except ValueError as exc:
        console.error(str(exc))
        return 1
    found = numbered(list_runs)
    shown = window(found, since, span, count)
    if not shown and (not found or args.failed or args.number is None):
        say_not_run(app_name, args, since, span)
        return 0
    if args.failed:
        if resolve is not None:
            resolve(shown)
        failed = [run for run in shown if run.status == "failed"]
        if not failed:
            console.say(f"{app_name} has no failed run in its last {len(shown)} runs.")
            return 0
        run = failed[0]
    else:
        if args.number is not None and not 1 <= args.number <= len(found):
            console.error(f"pdt runs {app_name} knows {len(found)} runs; "
                          f"pick a number from 1 to {len(found)}")
            return 1
        run = shown[0] if args.number is None else found[args.number - 1]
        if resolve is not None:
            resolve([run])
    lines = log_lines(read_lines, run, args.errors)
    total = len(lines)
    if not args.full:
        keep = args.lines or TAIL_LINES
        lines = lines[:keep] if args.head else lines[-keep:]
    if args.json:
        console.say(json.dumps([{"time": line.time.isoformat() if line.time else None,
                                 "level": line.level, "message": line.message}
                                for line in lines]))
        return 1 if run.status == "failed" else 0
    console.heading(run_heading(run, app_name))
    if len(lines) < total:
        side = "first" if args.head else "last"
        console.status(f"the {side} {len(lines)} of {total} lines; add --full for all of them")
    for line in lines:
        print_line(line)
    if args.follow:
        return follow(list_runs, read_lines, resolve, run, app_name, args.errors, delay)
    say_lag(run, app_name, total, store, delay)
    return 1 if run.status == "failed" else 0


def log_lines(read_lines: Callable[[Run], list[Line]], run: Run, errors: bool) -> list[Line]:
    lines = in_time_order([line for line in read_lines(run)
                           if not line.message.startswith(EXIT_MARKER)])
    if errors:
        lines = [line for line in lines if line.level not in ("DEBUG", "INFO")]
    return lines


def run_heading(run: Run, app_name: str) -> str:
    exit_part = "" if run.exit_code is None else f", exit {run.exit_code}"
    return (f"run {run.number} of {app_name}: started {started_text(run)}, "
            f"{duration_text(run)}, {run.status}{exit_part}")


def print_line(line: Line) -> None:
    console.log_line(local_text(line.time, "%H:%M:%S") if line.time else "",
                     line.level, line.message)


def minutes_text(delay: timedelta) -> str:
    minutes = max(1, round(delay.total_seconds() / 60))
    return "1 minute" if minutes == 1 else f"{minutes} minutes"


def say_lag(run: Run, app_name: str, total: int, store: str, delay: timedelta) -> None:
    """Tell the user when the lines printed may not be all the run's lines yet."""
    again = f"pdt logs {app_name} {run.number}"
    if run.status == "running":
        if total == 0:
            console.note(f"run {run.number} is still running, and {store} has no lines "
                         "from it yet.")
        else:
            console.note(f"run {run.number} is still running, so more lines will come.")
        console.command(f"{again} --follow", "print each line as it arrives")
        return
    settled = None if run.ended is None else run.ended + delay
    if settled is not None and datetime.now(UTC) < settled:
        console.note(f"run {run.number} ended at {local_text(run.ended, '%H:%M:%S')}, and "
                     f"{store} can take up to {minutes_text(delay)} to show a line, so "
                     f"{'no lines are' if total == 0 else 'the last lines may not be'} "
                     "there yet.")
        console.command(f"{again} --follow",
                        f"wait for them, until {local_text(settled, '%H:%M:%S')}")
    elif total == 0:
        console.note(f"{store} has no lines from run {run.number}.")


def follow(list_runs: Callable[[], list[Run]], read_lines: Callable[[Run], list[Line]],
           resolve: Callable[[list[Run]], None] | None, run: Run, app_name: str,
           errors: bool, delay: timedelta) -> int:
    """Print each new line of `run` until it has ended and `delay` has passed since then."""
    # A count per distinct line, so a line the job printed twice prints twice, and a
    # line the store shows late, between two older ones, still prints.
    seen = Counter((line.time, line.level, line.message)
                   for line in log_lines(read_lines, run, errors))
    console.status(f"waiting for new lines of run {run.number}; press Ctrl+C to stop")
    try:
        while run.status == "running" or run.ended is None or (
                datetime.now(UTC) < run.ended + delay):
            time.sleep(FOLLOW_SECONDS)
            current = next((item for item in list_runs() if item.id == run.id), None)
            if current is not None:
                if resolve is not None:
                    resolve([current])
                run = dataclasses.replace(current, number=run.number)
            now_seen = Counter()
            for line in log_lines(read_lines, run, errors):
                key = (line.time, line.level, line.message)
                now_seen[key] += 1
                if now_seen[key] > seen[key]:
                    seen[key] = now_seen[key]
                    print_line(line)
            if run.status != "running" and run.ended is None:
                break
    except KeyboardInterrupt:
        console.say()
        return 130
    console.heading(run_heading(run, app_name))
    return 1 if run.status == "failed" else 0


def health_row(app_name: str, found: list[Run] | None) -> dict:
    if found is None:
        return {"app": app_name, "status": "unknown", "last_run": None,
                "succeeded": 0, "runs": 0}
    if not found:
        return {"app": app_name, "status": "not yet run", "last_run": None,
                "succeeded": 0, "runs": 0}
    found = found[:DEFAULT_RUNS]
    status = {"succeeded": "ok"}.get(found[0].status, found[0].status)
    return {"app": app_name, "status": status, "last_run": found[0].started.isoformat(),
            "succeeded": sum(run.status == "succeeded" for run in found), "runs": len(found)}


def health(app_runs: dict[str, list[Run] | None], as_json: bool) -> int:
    """One row per app. `None` stands for an app whose runs could not be read."""
    rows = [health_row(app_name, found) for app_name, found in app_runs.items()]
    if as_json:
        console.say(json.dumps(rows))
    else:
        console.table(
            ["App", "Status", "Last run", "Recent"],
            [[row["app"], row["status"],
              local_text(datetime.fromisoformat(row["last_run"]))
              if row["last_run"] else "",
              f"{row['succeeded']} of {row['runs']} succeeded" if row["runs"] else ""]
             for row in rows],
            row_styles=[["bold cyan", console.RUN_STATUS_COLOURS[row["status"]]] for row in rows])
    return 1 if any(row["status"] in ("failed", "unknown") for row in rows) else 0
