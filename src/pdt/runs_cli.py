"""CLI helpers behind `pdt runs`, `pdt logs`, and `pdt health`.

Azure, Google Cloud, and AWS pass a live `stream`; the store reads are for ended
runs and for the fallback. A run's number is its place in the full list, so a
window never renumbers.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import re
import subprocess
import sys
import time
import webbrowser
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Callable, Iterable, Iterator

from pdt import console

DEFAULT_RUNS = 10
TAIL_LINES = 20
FOLLOW_SECONDS = 10
NEW_RUN_TRIES = 6
# A quiet live tail ends after this many seconds, so follow re-reads the run's status.
STREAM_QUIET = 60
# https://learn.microsoft.com/en-us/azure/azure-monitor/logs/data-ingestion-time
LOG_DELAY = timedelta(minutes=5)
SINCE_UNITS = {"h": "hours", "d": "days", "w": "weeks"}
SINCE_FORMS = ("a count with a unit (12h, 3d, 2w), a date (2026-09-20), "
               "or a date and time (2026-09-20T14:00)")
EXIT_MARKER = "pdt: exit "
LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
TEXT_LINE = re.compile(r"^\d\d:\d\d:\d\d (DEBUG|INFO|WARNING|ERROR)\s+(.*)$")
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


@dataclasses.dataclass
class LogPlace:
    """Where `pdt logs` reads each run's log: `shared` for every run, under `label`, and
    `items(runs)` for each run, in order, under `column`. A `folder` on this computer holds a
    file per run, named by its item. `--open` opens `page()`, or `link(item)` for one run."""
    label: str
    shared: str
    column: str
    items: Callable[[list[Run]], list[str]]
    page: Callable[[], str]
    link: Callable[[str], str]
    folder: Path | None = None
    note: str = ""


class StreamError(Exception):
    pass


def parse_line(raw: str, time: datetime | None) -> Line:
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
    keyed = []
    last = None
    for index, line in enumerate(lines):
        if line.time is not None:
            last = line.time
        keyed.append((last or datetime.min.replace(tzinfo=UTC), index, line))
    keyed.sort(key=lambda item: item[:2])
    return [line for _, _, line in keyed]


def json_line(record: dict, time: datetime | None) -> Line:
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
    code = exit_code(lines)
    if code is not None:
        return "succeeded" if code == 0 else "failed"
    return "running" if running() else "failed"


def run_json(run: Run) -> dict:
    return {"id": run.id, "started": run.started.isoformat(),
            "ended": run.ended.isoformat() if run.ended else None, "status": run.status,
            "exit_code": run.exit_code, "number": run.number}


def parse_runs(output: str) -> list[Run] | None:
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
    match = re.fullmatch(r"(\d+)([hdw])", value)
    if match is None:
        return None
    return timedelta(**{SINCE_UNITS[match.group(2)]: int(match.group(1))})


def parse_since(value: str, now: datetime) -> datetime:
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
        console.say(f"{console.value(app_name)} has not run yet.")
        console.command(f"pdt run {app_name} --deployed", "start one run now")
    elif span is None:
        console.say(f"{console.value(app_name)} has not run since {console.value(args.since)}.")
    else:
        console.say(f"{console.value(app_name)} has not run between {local_text(since)} "
                    f"and {local_text(since + span)}.")


def open_place(target: str, note: str = "") -> int:
    """Open a console URL in the browser, or a file or folder on this computer in its app."""
    if Path(target).is_absolute() and not Path(target).exists():
        console.error(f"{console.value(target)} does not exist yet; deploy and run the app first")
        return 1
    try:
        if target.startswith("https://"):
            if not webbrowser.open(target):
                console.field("open this page in a browser", target)
                return 0
        elif sys.platform == "win32":
            os.startfile(target)
        else:
            subprocess.run(["open" if sys.platform == "darwin" else "xdg-open", target],
                           check=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        console.error(f"pdt cannot open {console.value(target)}: {console.escape(str(exc))}")
        return 1
    console.done(f"opened {console.value(target)}")
    if note:
        console.say(note)
    return 0


def numbered(list_runs: Callable[[], list[Run]]) -> list[Run]:
    return [dataclasses.replace(run, number=number) for number, run in enumerate(list_runs(), 1)]


def runs(list_runs: Callable[[], list[Run]], app_name: str, rest: list[str],
         resolve: Callable[[list[Run]], None] | None = None, page: str = "",
         note: str = "") -> int:
    """`page` is where `--open` goes; `note` tells the user where to look on it."""
    parser = argparse.ArgumentParser(prog=f"pdt runs {app_name}")
    add_window_arguments(parser)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--open", action="store_true")
    args = parser.parse_args(rest)
    if args.open:
        return open_place(page, note)
    try:
        since, span, count = parse_window(args, datetime.now(UTC))
    except ValueError as exc:
        console.error(console.escape(str(exc)))
        return 1
    found = window(numbered(list_runs), since, span, count)
    if resolve is not None:
        resolve(found)
    if args.json:
        console.data(json.dumps([run_json(run) for run in found]))
        return 0
    if not found:
        say_not_run(app_name, args, since, span)
        return 0
    rows = [[str(run.number), started_text(run), duration_text(run), run.status,
             "-" if run.exit_code is None else str(run.exit_code), run.id] for run in found]
    row_styles = [["bold", "", "", console.RUN_STATUS_COLOURS[run.status], "", "bold"]
                  for run in found]
    console.table(["#", "Started", "Duration", "Status", "Exit Code", "Id"], rows,
                  row_styles=row_styles)
    return 0


def logs(list_runs: Callable[[], list[Run]],
         read_lines: Callable[[Run, datetime | None], list[Line]],
         app_name: str, rest: list[str],
         resolve: Callable[[list[Run]], None] | None = None,
         store: str = "the log", delay: timedelta = timedelta(0),
         hold: timedelta = timedelta(0),
         read_many: Callable[[list[Run]], dict[str, list[Line]]] | None = None,
         stream: Callable[[Run, bool], Iterable[Line]] | None = None,
         new: Callable[[Run], bool] | None = None,
         place: LogPlace | None = None) -> int:
    """`new` picks the run a start just made; the list may show it a few seconds late.
    `place` says where each run's log is, for --list."""
    parser = argparse.ArgumentParser(prog=f"pdt logs {app_name}")
    parser.add_argument("number", nargs="?", type=int)
    parser.add_argument("--id", action="append")
    parser.add_argument("--failed", action="store_true")
    parser.add_argument("--errors", action="store_true")
    parser.add_argument("--lines", type=int)
    parser.add_argument("--head", action="store_true")
    parser.add_argument("--full", action="store_true")
    parser.add_argument("--follow", action="store_true")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--open", action="store_true")
    add_window_arguments(parser)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(rest)
    if args.open and args.number is None and not args.id and not args.failed:
        return open_place(place.page(), place.note)
    if args.list and (args.follow or args.id or args.failed):
        console.error("--list lists the log files; drop --follow, --id, and --failed")
        return 1
    if args.full and (args.lines is not None or args.head):
        console.error("--full prints every line; drop --lines and --head")
        return 1
    if args.follow and (args.json or args.head):
        console.error("--follow prints lines as they arrive; drop --json and --head")
        return 1
    if args.follow and len(args.id or []) > 1:
        console.error("--follow prints one run's lines as they arrive; name one --id")
        return 1
    if args.lines is not None and args.lines < 1:
        console.error("--lines must be 1 or more")
        return 1
    try:
        since, span, count = parse_window(args, datetime.now(UTC))
    except ValueError as exc:
        console.error(console.escape(str(exc)))
        return 1
    if args.list:
        return list_logs(window(numbered(list_runs), since, span, count), place, app_name,
                         args, since, span)
    chosen = []
    with console.waiting("reading the run list") as wait:
        found = numbered(list_runs)
        for attempt in range(NEW_RUN_TRIES if new is not None else 0):
            if attempt > 0:
                wait.now("waiting for the new run to show in the run list")
                time.sleep(FOLLOW_SECONDS)
                found = numbered(list_runs)
            chosen = [run for run in found if new(run)][:1]
            if chosen:
                break
    shown = window(found, since, span, count)
    if new is not None:
        if not chosen:
            console.note("the run list does not show the new run yet.")
            console.command(f"pdt logs {app_name} --follow", "print its lines as they arrive")
            return 0
        if resolve is not None:
            resolve(chosen)
    elif args.id:
        chosen = []
        for wanted in args.id:
            run = next((run for run in found if run.id == wanted), None)
            if run is None:
                console.error(f"pdt runs {console.value(app_name)} knows no run with id "
                              f"{console.value(wanted)}")
                return 1
            chosen.append(run)
        if resolve is not None:
            resolve(chosen)
    elif not shown and (not found or args.failed or args.number is None):
        say_not_run(app_name, args, since, span)
        if args.json:
            console.data("[]")
        return 0
    elif args.failed:
        if resolve is not None:
            resolve(shown)
        failed = [run for run in shown if run.status == "failed"]
        if not failed:
            console.say(f"{console.value(app_name)} has no failed run in its last "
                        f"{len(shown)} runs.")
            if args.json:
                console.data("[]")
            return 0
        chosen = [failed[0]]
    else:
        if args.number is not None and not 1 <= args.number <= len(found):
            console.error(f"pdt runs {console.value(app_name)} knows {len(found)} runs; "
                          f"pick a number from 1 to {len(found)}")
            return 1
        chosen = [shown[0] if args.number is None else found[args.number - 1]]
        if resolve is not None:
            resolve(chosen)
    if args.open:
        return max(open_place(place.link(item), place.note) for item in place.items(chosen))
    if place is not None and place.folder is not None:
        for item in place.items(chosen):
            console.status(f"reading {console.value(place.folder / item)}")
    lines_by_id = {}
    live = set()
    with console.waiting("reading the log"):
        for run in chosen:
            if stream is not None and run.status == "running":
                try:
                    lines_by_id[run.id] = list(stream(run, False))
                    live.add(run.id)
                except StreamError as exc:
                    console.warn(f"pdt cannot read the run's live log, so it reads {store}: "
                                 f"{console.escape(str(exc))}")
        stored = [run for run in chosen if run.id not in live]
        if stored and read_many is not None:
            lines_by_id.update(read_many(stored))
        else:
            lines_by_id.update({run.id: read_lines(run, None) for run in stored})
    now = datetime.now(UTC)
    output = {}
    results = []
    for run in chosen:
        raw = lines_by_id.get(run.id, [])
        if args.follow and run.id not in live:
            raw = [line for line in raw if line.time is None or line.time <= now - hold]
        run = ended_by(run, last_exit_line(raw))
        results.append(run)
        lines = log_lines(raw, args.errors)
        total = len(lines)
        if not args.full:
            keep = args.lines or TAIL_LINES
            lines = lines[:keep] if args.head else lines[-keep:]
        if args.json:
            output[run.id] = [{"time": line.time.isoformat() if line.time else None,
                               "level": line.level, "message": line.message}
                              for line in lines]
            continue
        finished = exit_code(raw) is not None
        settled = settles(run, finished, delay)
        following = args.follow and (run.status == "running"
                                     or (settled is not None and now < settled))
        console.heading(console.escape(run_heading(run, app_name, outcome=not following)))
        if following and run.status == "running":
            console.status(f"watching for log output for {console.value(app_name)}; "
                           "press Ctrl+C to stop, the run keeps going")
        elif following:
            console.status(f"the run ended at {local_text(run.ended, '%H:%M:%S')}, and {store} "
                           f"can take up to {minutes_text(delay)} to show its last lines, so pdt "
                           f"waits until {local_text(settled, '%H:%M:%S')} at the latest; "
                           "press Ctrl+C to stop")
        if len(lines) < total:
            side = "first" if args.head else "last"
            console.status(f"the {side} {len(lines)} of {total} lines; add --full for all of them")
        for line in lines:
            print_line(line)
        if following:
            return follow(list_runs, read_lines, resolve, run, args.errors, delay, raw,
                          hold=hold, stream=stream, store=store)
        say_lag(run, app_name, total, None if run.id in live else store, delay, finished,
                args.errors)
    if args.json:
        console.data(json.dumps(output if len(args.id or []) > 1 else output[chosen[0].id]))
    return 1 if any(run.status == "failed" for run in results) else 0


def list_logs(found: list[Run], place: LogPlace, app_name: str, args: argparse.Namespace,
              since: datetime | None, span: timedelta | None) -> int:
    records = []
    for run, item in zip(found, place.items(found)):
        record = dict(run_json(run), location=item)
        if place.folder is not None:
            path = place.folder / item
            try:
                size = path.stat().st_size
            except FileNotFoundError:
                size = 0
            record.update(file=str(path), size=size)
        records.append(record)
    if args.json:
        console.data(json.dumps(records))
        return 0
    if not found:
        say_not_run(app_name, args, since, span)
        return 0
    console.field(place.label, place.shared)
    headers = ["#", place.column, "Started", "Exit Code"]
    if place.folder is not None:
        headers.append("Size")
    rows = []
    for run, record in zip(found, records):
        code = "-" if run.exit_code is None else str(run.exit_code)
        row = [str(run.number), record["location"], started_text(run),
               "running" if run.exit_code is None and run.status == "running" else code]
        if place.folder is not None:
            row.append(console.size(record["size"]))
        rows.append(row)
    console.table(headers, rows, ["bold", "bold"])
    return 0


def log_lines(lines: list[Line], errors: bool) -> list[Line]:
    lines = in_time_order(lines)
    if errors:
        lines = [line for line in lines if line.level not in ("DEBUG", "INFO")]
    return lines


def run_heading(run: Run, app_name: str, outcome: bool = True) -> str:
    heading = f"run {run.number} of {app_name}: started {started_text(run)}"
    if not outcome:
        return heading
    exit_part = "" if run.exit_code is None else f", exit {run.exit_code}"
    duration = "" if run.ended is None else f"{duration_text(run)}, "
    return f"{heading}, {duration}{run.status}{exit_part}"


def say_result(run: Run) -> None:
    after = "" if run.ended is None else f" after {duration_text(run)}"
    exit_part = "" if run.exit_code is None else f", exit {run.exit_code}"
    result_text = f"the run {run.status}{after}{exit_part}"
    if run.status == "succeeded":
        console.done(result_text)
    else:
        console.failed(result_text)


def print_line(line: Line) -> None:
    console.log_line(local_text(line.time, "%H:%M:%S") if line.time else "",
                     line.level, line.message)


def minutes_text(delay: timedelta) -> str:
    minutes = max(1, round(delay.total_seconds() / 60))
    return "1 minute" if minutes == 1 else f"{minutes} minutes"


def settles(run: Run, finished: bool, delay: timedelta) -> datetime | None:
    return None if run.ended is None or finished else run.ended + delay


def say_lag(run: Run, app_name: str, total: int, store: str | None, delay: timedelta,
            finished: bool, errors: bool = False) -> None:
    again = f"pdt logs {app_name} {run.number}"
    kind = "warning or error lines" if errors else "lines"
    if run.status == "running":
        if total == 0 and store is None:
            console.note(f"run {console.value(run.number)} is still running, "
                         f"and has written no {kind} yet.")
        elif total == 0:
            console.note(f"run {console.value(run.number)} is still running, and {store} "
                         f"has no {kind} from it yet.")
        else:
            console.note(f"run {console.value(run.number)} is still running, "
                         "so more lines will come.")
        console.command(f"{again} --follow", "print each line as it arrives")
        return
    settled = settles(run, finished, delay)
    if settled is not None and datetime.now(UTC) < settled:
        console.note(f"run {console.value(run.number)} ended at "
                     f"{local_text(run.ended, '%H:%M:%S')}, and "
                     f"{store} can take up to {minutes_text(delay)} to show a line, so "
                     f"{f'no {kind} are' if total == 0 else 'the last lines may not be'} "
                     "there yet.")
        console.command(f"{again} --follow",
                        f"wait for them, until {local_text(settled, '%H:%M:%S')}")
    elif total == 0 and store is None:
        console.note(f"run {console.value(run.number)} wrote no {kind}.")
    elif total == 0:
        console.note(f"{store} has no {kind} from run {console.value(run.number)}.")


def latest(list_runs: Callable[[], list[Run]], resolve: Callable[[list[Run]], None] | None,
           run: Run) -> Run:
    current = next((item for item in list_runs() if item.id == run.id), None)
    if current is None:
        return run
    if resolve is not None:
        resolve([current])
    return dataclasses.replace(current, number=run.number)


def line_key(line: Line) -> tuple:
    return (line.time, line.level, line.message)


def last_exit_line(lines: list[Line]) -> Line | None:
    return next((line for line in reversed(lines) if line.message.startswith(EXIT_MARKER)), None)


def unseen(lines: list[Line], errors: bool, seen: Counter) -> list[Line]:
    now_seen = Counter()
    new = []
    for line in log_lines(lines, errors):
        key = line_key(line)
        now_seen[key] += 1
        if now_seen[key] > seen[key]:
            seen[key] = now_seen[key]
            new.append(line)
    return new


def store_and_tail(read: Callable[[datetime | None], list[Line]], tail: Iterable[Line],
                   hold: timedelta) -> Iterator[Line]:
    """The store's lines, then each new tail line, until the exit line. A second store read
    from `hold` before the tail opened gives the lines the store showed late."""
    opened = datetime.now(UTC)
    printed = Counter()
    lines = unseen(read(None), False, printed)
    yield from lines
    if last_exit_line(lines) is not None:
        return
    reread = False
    tailed = Counter()
    for line in tail:
        exit_line = line.message.startswith(EXIT_MARKER)
        if not reread and (exit_line or datetime.now(UTC) >= opened + hold):
            reread = True
            lines = unseen(read(opened - hold), False, printed)
            yield from lines
            if last_exit_line(lines) is not None:
                return
        key = line_key(line)
        tailed[key] += 1
        if tailed[key] > printed[key]:
            printed[key] = tailed[key]
            yield line
        if exit_line:
            return
    if not reread:
        yield from unseen(read(opened - hold), False, printed)


def follow(list_runs: Callable[[], list[Run]],
           read_lines: Callable[[Run, datetime | None], list[Line]],
           resolve: Callable[[list[Run]], None] | None, run: Run, errors: bool,
           delay: timedelta, shown: list[Line], hold: timedelta = timedelta(0),
           stream: Callable[[Run, bool], Iterable[Line]] | None = None,
           store: str = "the log") -> int:
    # Counts, not a set: a line the job wrote twice prints twice; a reconnected stream's replay prints once.
    seen = Counter(line_key(line) for line in shown)
    try:
        with console.waiting("waiting for the run's next line") as wait:
            streamed = (follow_stream(list_runs, resolve, run, errors, stream, store, seen,
                                      last_exit_line(shown), wait)
                        if stream is not None and run.status == "running" else None)
            run = streamed or follow_store(list_runs, read_lines, resolve, run, errors, delay,
                                           hold, store, seen, wait)
    except KeyboardInterrupt:
        console.say()
        return 130
    say_result(run)
    return 1 if run.status == "failed" else 0


def follow_stream(list_runs: Callable[[], list[Run]],
                  resolve: Callable[[list[Run]], None] | None, run: Run, errors: bool,
                  stream: Callable[[Run, bool], Iterable[Line]], store: str,
                  seen: Counter, exit_line: Line | None, wait: console.Wait) -> Run | None:
    """Prints the run's live log until the run ends; the ended run, or None when the live
    log failed or showed no line of it. The live log is read once more after the run is
    seen to end, so a short run that starts and ends between two reads still shows."""
    try:
        while exit_line is None:
            ended = run.status != "running"
            wait.now("waiting for the run's next line in its live log")
            opened = time.monotonic()
            now_seen = Counter()
            for line in stream(run, not ended):
                key = line_key(line)
                now_seen[key] += 1
                if now_seen[key] <= seen[key]:
                    continue
                seen[key] = now_seen[key]
                if line.message.startswith(EXIT_MARKER):
                    exit_line = line
                if not errors or line.level not in ("DEBUG", "INFO"):
                    print_line(line)
            if ended:
                break
            run = latest(list_runs, resolve, run)
            if (run.status == "running" and exit_line is None
                    and time.monotonic() - opened < FOLLOW_SECONDS):
                time.sleep(FOLLOW_SECONDS)
    except StreamError as exc:
        console.warn(f"pdt cannot read the run's live log, so it reads {store}: "
                     f"{console.escape(str(exc))}")
        return None
    return ended_by(run, exit_line) if seen else None


def ended_by(run: Run, exit_line: Line | None) -> Run:
    """A provider shows a run's end some seconds after the job writes its exit line."""
    if exit_line is None:
        return run
    code = exit_code([exit_line])
    if run.status == "running":
        run = dataclasses.replace(run, status="succeeded" if code == 0 else "failed",
                                  ended=exit_line.time or datetime.now(UTC))
    if run.exit_code is None:
        run = dataclasses.replace(run, exit_code=code)
    return run


def follow_store(list_runs: Callable[[], list[Run]],
                 read_lines: Callable[[Run, datetime | None], list[Line]],
                 resolve: Callable[[list[Run]], None] | None, run: Run, errors: bool,
                 delay: timedelta, hold: timedelta, store: str, seen: Counter,
                 wait: console.Wait) -> Run:
    """A line prints once it is `hold` old, so a line the store shows late still prints
    in time order. The extra read after the exit line catches a line later than `hold`."""
    held = []
    since = None
    first = True
    while True:
        wait.now(f"reading {store} every {FOLLOW_SECONDS}s for the run's next line"
                 if run.status == "running" else f"waiting for {store} to show the run's last lines")
        raw = read_lines(run, since)
        now = datetime.now(UTC)
        exit_line = last_exit_line(raw)
        done = (exit_line is not None and (
            exit_line.time is None or exit_line.time <= now - hold)) or (
            run.status != "running" and (run.ended is None or now >= run.ended + delay))
        times = [line.time for line in raw
                 if line.time is not None and (done or line.time <= now - hold)]
        if times:
            since = max(times) - hold
        held += unseen(raw, errors, seen)
        ready, waiting = [], []
        for line in held:
            if done or line.time is None or line.time <= now - hold:
                ready.append(line)
            else:
                waiting.append(line)
        held = waiting
        for line in in_time_order(ready):
            print_line(line)
        if done:
            if exit_line is not None and hold and not first:
                wait.now(f"waiting {FOLLOW_SECONDS}s for lines {store} shows late")
                time.sleep(FOLLOW_SECONDS)
                for line in unseen(read_lines(run, since), errors, seen):
                    print_line(line)
            if exit_line is None:
                console.note(f"{store} does not show the run's exit line, so its log may "
                             "end early.")
            return ended_by(run, exit_line)
        first = False
        time.sleep(FOLLOW_SECONDS)
        run = latest(list_runs, resolve, run)


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
    rows = [health_row(app_name, found) for app_name, found in app_runs.items()]
    if as_json:
        console.data(json.dumps(rows))
    else:
        console.table(
            ["App", "Status", "Last run", "Recent"],
            [[row["app"], row["status"],
              local_text(datetime.fromisoformat(row["last_run"]))
              if row["last_run"] else "",
              f"{row['succeeded']} of {row['runs']} succeeded" if row["runs"] else ""]
             for row in rows],
            row_styles=[["bold", console.RUN_STATUS_COLOURS[row["status"]]] for row in rows])
    return 1 if any(row["status"] in ("failed", "unknown") for row in rows) else 0
