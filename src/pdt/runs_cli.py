"""CLI helpers behind `pdt runs`, `pdt logs`, and `pdt health`.

Every provider gives this module two callables and nothing else:
`list_runs()` returns the app's runs, newest first, at most RUN_HISTORY,
and `read_lines(run)` returns one run's log lines, oldest first.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import re
from datetime import UTC, datetime, timedelta
from typing import Callable

from pdt import console

RUN_HISTORY = 50
DEFAULT_RUNS = 10
SINCE_UNITS = {"h": "hours", "d": "days", "w": "weeks"}
SINCE_FORMS = ("a count with a unit (12h, 3d, 2w), a date (2026-09-20), "
               "or a date and time (2026-09-20T14:00)")
EXIT_MARKER = "pdt: exit "
LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
TEXT_LINE = re.compile(r"^\d\d:\d\d:\d\d (DEBUG|INFO|WARNING|ERROR)\s+(.*)$")


@dataclasses.dataclass
class Run:
    id: str
    started: datetime
    ended: datetime | None
    status: str
    exit_code: int | None = None


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
    return Line(time, "", raw)


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
            "exit_code": run.exit_code}


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
                record["status"], record["exit_code"]) for record in records]


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


def parse_since(value: str, now: datetime) -> datetime:
    """The moment `--since VALUE` names; a date without a time is local midnight."""
    match = re.fullmatch(r"(\d+)([hdw])", value)
    if match:
        return now - timedelta(**{SINCE_UNITS[match.group(2)]: int(match.group(1))})
    for form in ("%Y-%m-%d", "%Y-%m-%dT%H:%M"):
        try:
            return datetime.strptime(value, form).astimezone()
        except ValueError:
            pass
    raise ValueError(f"--since {value} is not a time pdt reads; use {SINCE_FORMS}")


def window(found: list[Run], since: datetime | None) -> list[Run]:
    """The runs `pdt runs` numbers: the DEFAULT_RUNS newest, or every run since a moment."""
    if since is None:
        return found[:DEFAULT_RUNS]
    return [run for run in found if run.started >= since]


def say_not_run(app_name: str, since: str | None) -> None:
    if since is None:
        console.say(f"{app_name} has not run yet.")
    else:
        console.say(f"{app_name} has not run since {since}.")


def runs(list_runs: Callable[[], list[Run]], app_name: str, rest: list[str]) -> int:
    parser = argparse.ArgumentParser(prog=f"pdt runs {app_name}")
    parser.add_argument("--since")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(rest)
    try:
        since = None if args.since is None else parse_since(args.since, datetime.now(UTC))
    except ValueError as exc:
        console.error(str(exc))
        return 1
    found = list_runs()
    if args.json:
        shown = found if since is None else window(found, since)
        console.say(json.dumps([run_json(run) for run in shown]))
        return 0
    found = window(found, since)
    if not found:
        say_not_run(app_name, args.since)
        return 0
    rows = [[str(number), started_text(run), duration_text(run), run.status,
             "-" if run.exit_code is None else str(run.exit_code), run.id]
            for number, run in enumerate(found, 1)]
    row_styles = [["", "", "", console.RUN_STATUS_COLOURS[run.status], "", "dim"]
                  for run in found]
    console.table(["#", "Started", "Duration", "Status", "Exit", "Id"], rows,
                  row_styles=row_styles)
    return 0


def logs(list_runs: Callable[[], list[Run]], read_lines: Callable[[Run], list[Line]],
         app_name: str, rest: list[str]) -> int:
    parser = argparse.ArgumentParser(prog=f"pdt logs {app_name}")
    parser.add_argument("number", nargs="?", type=int, default=1)
    parser.add_argument("--failed", action="store_true")
    parser.add_argument("--errors", action="store_true")
    parser.add_argument("--since")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(rest)
    try:
        since = None if args.since is None else parse_since(args.since, datetime.now(UTC))
    except ValueError as exc:
        console.error(str(exc))
        return 1
    found = window(list_runs(), since)
    if not found:
        say_not_run(app_name, args.since)
        return 0
    if args.failed:
        failed = [number for number, run in enumerate(found, 1) if run.status == "failed"]
        if not failed:
            console.say(f"{app_name} has no failed run in its last {len(found)} runs.")
            return 0
        number = failed[0]
    else:
        number = args.number
        if not 1 <= number <= len(found):
            console.error(f"pdt runs {app_name} lists {len(found)} runs; "
                          f"pick a number from 1 to {len(found)}")
            return 1
    run = found[number - 1]
    lines = [line for line in read_lines(run) if not line.message.startswith(EXIT_MARKER)]
    if args.errors:
        lines = [line for line in lines if line.level not in ("DEBUG", "INFO")]
    if args.json:
        console.say(json.dumps([{"time": line.time.isoformat() if line.time else None,
                                 "level": line.level, "message": line.message}
                                for line in lines]))
    else:
        exit_part = "" if run.exit_code is None else f", exit {run.exit_code}"
        console.heading(f"run {number} of {app_name}: started {started_text(run)}, "
                        f"{duration_text(run)}, {run.status}{exit_part}")
        for line in lines:
            console.log_line(local_text(line.time, "%H:%M:%S") if line.time else "",
                             line.level, line.message)
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
