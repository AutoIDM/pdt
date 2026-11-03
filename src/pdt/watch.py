"""Keep a history of each top-level pdt command, and point a person at the pdt
community after a pdt command fails at a terminal.

The history in `HISTORY` stays on this computer. Each entry holds the shape of the command
(pdt's command and flag names, every other word `<value>`), the exception type, pdt's own
lines in the traceback, and the error message pdt printed. `pdt support` reads it, and only
a coding agent the user allowed sees the message.
When pdt itself crashes at a terminal, `pdt support` starts on its own. It sends
nothing anywhere.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from uuid import uuid4

from pdt import config

SUPPORT_URL = "https://autoidm.com/discord"
COMMUNITY = "Get help and share ideas in the pdt community on Discord:"
HISTORY = config.data_home() / "pdt" / "history.jsonl"
NOTE_ENV = "PDT_WATCH_NOTE"
OFF_ENV = "PDT_ERROR_WATCH"
SETTING = "error-watch"
WINDOW = timedelta(hours=24)
TAIL_BYTES = 262144
AGENT_ENVS = ("CLAUDECODE", "CURSOR_AGENT", "CODEX_SANDBOX", "GEMINI_CLI")
PACKAGE = Path(__file__).resolve().parent
TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
NOTE_RANKS = {"unhandled": 2, "error": 1, "usage": 1, "interrupted": 1}
TRACE_FRAMES = 10
MESSAGE_CHARS = 2000
VALUE = "<value>"

_seen: tuple[str, str, BaseException | None, list] = ("", "", None, [])


def saw(message: str, usage: bool = False) -> None:
    """Keep the last error pdt printed, with the exception that pdt was handling then, such
    as the ConfigError that `cli.run` prints, or else the calls that led to the print."""
    global _seen
    error = sys.exc_info()[1]
    calls = traceback.extract_stack()[:-2] if error is None else []
    _seen = ("usage" if usage else "", message, error, calls)


def is_flag(token: str) -> bool:
    long_flag = token.startswith("--")
    short_flag = token.startswith("-") and len(token) == len("-x")
    return long_flag or short_flag


def command_type(argv: list[str], commands: set[str]) -> str:
    word = next((token for token in argv[1:] if not is_flag(token)), "")
    return f"pdt {word}" if word in commands else "pdt"


def command_index(argv: list[str], commands: set[str]) -> int | None:
    """Where the pdt command is in argv: the first word that is not a flag, when it is a
    command. A later word that matches a command is something the user typed."""
    first = next((index for index, token in enumerate(argv) if index > 0 and not is_flag(token)),
                 None)
    return first if first is not None and argv[first] in commands else None


def shape(argv: list[str], commands: set[str], flags: set[str] = frozenset()) -> str:
    words = ["pdt"]
    command = command_index(argv, commands)
    for index, token in enumerate(argv[1:], 1):
        flag, equals, _ = token.partition("=")
        if index == command:
            words.append(token)
        elif flag in flags:
            words.append(flag + ("=<value>" if equals else ""))
        else:
            words.append(VALUE)
    return " ".join(words)


def where(frames: list[traceback.FrameSummary]) -> list[str]:
    """pdt's own lines in `frames`, such as `config.py:663 in cron_expression`."""
    lines = []
    for frame in frames:
        path = Path(frame.filename).resolve()
        if path.is_relative_to(PACKAGE) and path != Path(__file__).resolve():
            lines.append(f"{path.relative_to(PACKAGE).as_posix()}:{frame.lineno} in {frame.name}")
    return lines[-TRACE_FRAMES:]


def failed(outcome: str, error: BaseException | None, message: str,
           calls: list[traceback.FrameSummary] = ()) -> dict:
    from rich.errors import MarkupError
    from rich.text import Text
    try:
        message = Text.from_markup(message).plain
    except MarkupError:
        pass
    return {"outcome": outcome, "error": "" if error is None else type(error).__name__,
            "where": where(calls if error is None else traceback.extract_tb(error.__traceback__)),
            "message": message.strip()[:MESSAGE_CHARS]}


def exit_code(code: object) -> int:
    if code is None:
        return 0
    return code if isinstance(code, int) else 1


def handled(code: int) -> dict:
    if code == 0:
        return failed("ok", None, "")
    if code == 130:
        outcome = "interrupted"
    else:
        outcome = "usage" if _seen[0] == "usage" else "error"
    return failed(outcome, _seen[2], _seen[1] or f"exit {code}", _seen[3])


def off() -> bool:
    if os.environ.get(OFF_ENV, "").strip().lower() in ("0", "false", "off", "no"):
        return True
    try:
        return config.load_yaml(config.find_project() / config.PROJECT_FILE).get(SETTING) is False
    except (config.ConfigError, OSError, UnicodeDecodeError):
        return False


def can_offer() -> bool:
    from pdt.utils.email_auth import can_prompt
    return (can_prompt(None) and sys.stderr.isatty()
            and not any(len(token) > 2 and "--json".startswith(token) for token in sys.argv)
            and not any(name in os.environ for name in AGENT_ENVS))


def project() -> str:
    """An id of the project folder, so the history holds no path."""
    try:
        folder = str(config.find_project())
    except (config.ConfigError, OSError):
        return ""
    return hashlib.sha256(folder.encode()).hexdigest()[:16]


def watched(main: Callable[[], int], commands: set[str] = frozenset(),
            flags: set[str] = frozenset()) -> int:
    # A provider script's environment has no argcomplete, so completion.py is not imported.
    completing = getattr(sys.modules.get("pdt.completion"), "COMPLETING", False)
    if completing or os.environ.get("CI", "").strip() != "" or off():
        return main()
    from pdt import console
    console.error_hook = saw
    if NOTE_ENV in os.environ:
        return child(main)
    note = HISTORY.parent / "notes" / f"{uuid4().hex}.json"
    os.environ[NOTE_ENV] = str(note)
    start = time.monotonic()
    result = 1
    stop = None
    failure = None
    try:
        result = main()
        code = exit_code(result)
    except SystemExit as e:
        stop = e
        code = exit_code(e.code)
    except Exception as e:
        traceback.print_exc()
        failure = e
        code = 1
    os.environ.pop(NOTE_ENV, None)
    entry = None
    try:
        found = failed("unhandled", failure, str(failure)) if failure is not None else handled(code)
        if note.is_file():
            try:
                child_note = json.loads(note.read_text())
            except ValueError:
                child_note = {}
            note.unlink()
            if code != 0 and child_note.get("outcome") in NOTE_RANKS:
                outcome = "unhandled" if child_note["outcome"] == "unhandled" else found["outcome"]
                found = {**child_note, "outcome": outcome}
        entry = {"v": 2, "time": datetime.now(timezone.utc).strftime(TIME_FORMAT),
                 "project": project(), "command": shape(sys.argv, commands, flags),
                 **found, "code": code, "seconds": round(time.monotonic() - start, 2),
                 "pdt": version(), "os": sys.platform, "python": platform.python_version()}
        record(entry)
    except Exception:
        pass
    if entry is not None and entry["outcome"] == "unhandled" and can_offer():
        from pdt import support
        support.crashed(entry, failure)
    else:
        offer(code, commands)
    if stop is not None:
        raise stop
    return 1 if failure is not None else result


def child(main: Callable[[], int]) -> int:
    try:
        result = main()
    except SystemExit as e:
        if exit_code(e.code) != 0:
            write_note(lambda code=exit_code(e.code): handled(code))
        raise
    except Exception as e:
        write_note(lambda error=e: failed("unhandled", error, str(error)))
        raise
    if exit_code(result) != 0:
        write_note(lambda: handled(exit_code(result)))
    return result


def write_note(make: Callable[[], dict]) -> None:
    """Write the failure that `make` gives to the parent's note. Nothing that goes wrong here
    changes the child's exit code or exception."""
    try:
        found = make()
        path = Path(os.environ[NOTE_ENV])
        if path.is_file():
            kept = json.loads(path.read_text()).get("outcome")
            if NOTE_RANKS.get(kept, 0) >= NOTE_RANKS[found["outcome"]]:
                return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(found))
    except Exception:
        pass


def version() -> str:
    from pdt import __version__
    return __version__


def record(entry: dict) -> None:
    HISTORY.parent.mkdir(parents=True, exist_ok=True)
    with HISTORY.open("a") as f:
        f.write(json.dumps(entry) + "\n")


def recent() -> list[dict]:
    if not HISTORY.is_file():
        return []
    with HISTORY.open("rb") as f:
        f.seek(0, os.SEEK_END)
        start = max(0, f.tell() - TAIL_BYTES)
        f.seek(start)
        lines = f.read().decode(errors="replace").splitlines()
    if start > 0:
        lines = lines[1:]
    entries = []
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    return entries


def within(entry: dict, now: datetime) -> bool:
    try:
        then = datetime.strptime(entry["time"], TIME_FORMAT).replace(tzinfo=timezone.utc)
    except (KeyError, TypeError, ValueError):
        return False
    return now - then <= WINDOW


def offer(code: int, commands: set[str]) -> None:
    from pdt import console
    from pdt.deploy import DECLINED
    if code in (0, DECLINED, 130) or not can_offer():
        return
    console.say(f"Get help with {console.escape(command_type(sys.argv, commands))} "
                f"at {console.value(SUPPORT_URL)}, or run {console.value('pdt support')} to "
                "report it to the pdt team")
