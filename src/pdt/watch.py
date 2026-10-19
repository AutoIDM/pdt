"""Keep a history of each top-level pdt command, and point at help from the pdt community.

The history in `HISTORY` holds no value the user typed. pdt prints where to get
help after repeated errors, only to a person at a terminal. It sends nothing anywhere.
"""

from __future__ import annotations

import json
import os
import platform
import re
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from uuid import uuid4

from pdt import config

SUPPORT_URL = "https://discord.gg/4j83nqnNfK"
HISTORY = config.data_home() / "pdt" / "history.jsonl"
NOTE_ENV = "PDT_WATCH_NOTE"
OFF_ENV = "PDT_ERROR_WATCH"
SETTING = "error-watch"
REPEATS = 3
WINDOW = timedelta(hours=24)
TAIL_BYTES = 262144
AGENT_ENVS = ("CLAUDECODE", "CURSOR_AGENT", "CODEX_SANDBOX", "GEMINI_CLI")
OUTCOMES = ("ok", "error", "usage", "unhandled", "interrupted")
ERRORS = ("error", "usage", "unhandled")
PACKAGE = Path(__file__).resolve().parent
TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
NOTE_RANKS = {"unhandled": 2, "error": 1, "usage": 1, "interrupted": 1}

_seen = ("", "")

LOCATIONS = (
    (re.compile(r"\b[A-Za-z][A-Za-z0-9+.-]*://\S+"), "<url>"),
    (re.compile(r"\b[A-Za-z]:\\[^\s'\"]*"), "<path>"),
    (re.compile(r"(?<![\w.~])/[^\s'\"]+"), "<path>"),
)

SCRUBS = (
    (re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"), "<email>"),
    (re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)=(?!<value>)\S+"), r"\1=<value>"),
    (re.compile(r"[A-Za-z0-9+/=_-]{16,}"),
     lambda match: "<id>" if re.search(r"\d", match.group()) else match.group()),
    (re.compile(r"\d+"), "#"),
)


def saw(message: str, usage: bool = False) -> None:
    global _seen
    _seen = ("usage" if usage else "", message)


def is_flag(token: str) -> bool:
    long_flag = token.startswith("--")
    short_flag = token.startswith("-") and len(token) == len("-x")
    return long_flag or short_flag


def shape(argv: list[str], commands: set[str]) -> str:
    words = ["pdt"]
    for token in argv[1:]:
        if token in commands:
            words.append(token)
        elif is_flag(token):
            flag, equals, _ = token.partition("=")
            words.append(flag + ("=<value>" if equals else ""))
        else:
            words.append("<arg>")
    return " ".join(words)


def signature(text: str, typed: list[str] = ()) -> str:
    from rich.errors import MarkupError
    from rich.text import Text
    try:
        text = Text.from_markup(text).plain
    except MarkupError:
        pass
    line = next((line.strip() for line in text.splitlines() if line.strip()), "")
    for pattern, replacement in LOCATIONS:
        line = pattern.sub(replacement, line)
    values = sorted({value for value in os.environ.values() if len(value) >= 6},
                    key=len, reverse=True)
    for value in values:
        line = line.replace(value, "<value>")
    for word in sorted(set(typed), key=len, reverse=True):
        line = re.sub(rf"(?<![\w-]){re.escape(word)}(?![\w-])", "<arg>", line)
    for pattern, replacement in SCRUBS:
        line = pattern.sub(replacement, line)
    return line[:160]


def exception_signature(error: BaseException, typed: list[str] = ()) -> str:
    text = f"{type(error).__name__}: {signature(str(error), typed)}"
    for frame in reversed(traceback.extract_tb(error.__traceback__)):
        path = Path(frame.filename).resolve()
        if path.is_relative_to(PACKAGE) and path != Path(__file__).resolve():
            return f"{text} at {path.relative_to(PACKAGE).as_posix()}:{frame.name}"
    return text


def exit_code(code: object) -> int:
    if code is None:
        return 0
    return code if isinstance(code, int) else 1


def handled(code: int, typed: list[str] = ()) -> tuple[str, str]:
    if code == 0:
        return "ok", ""
    if code == 130:
        outcome = "interrupted"
    else:
        outcome = "usage" if _seen[0] == "usage" else "error"
    return outcome, signature(_seen[1], typed) or f"exit {code}"


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


def typed_words(argv: list[str], commands: set[str]) -> list[str]:
    words = []
    for token in argv[1:]:
        if is_flag(token):
            words.append(token.partition("=")[2])
        elif token not in commands:
            words.append(token)
    return [word for word in words if word != ""]


def project() -> str:
    try:
        return str(config.find_project())
    except (config.ConfigError, OSError):
        return ""


def watched(main: Callable[[], int], commands: set[str] = frozenset()) -> int:
    from pdt import completion
    if completion.COMPLETING or os.environ.get("CI", "").strip() != "" or off():
        return main()
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
    try:
        typed = typed_words(sys.argv, commands)
        if failure is not None:
            outcome, sign = "unhandled", exception_signature(failure, typed)
        else:
            outcome, sign = handled(code, typed)
        if note.is_file():
            try:
                child_note = json.loads(note.read_text())
            except ValueError:
                child_note = {}
            note.unlink()
            if code != 0 and child_note.get("signature"):
                sign = signature(child_note["signature"], typed)
                if child_note.get("outcome") == "unhandled":
                    outcome = "unhandled"
        record({"v": 1, "time": datetime.now(timezone.utc).strftime(TIME_FORMAT),
                "project": project(), "command": shape(sys.argv, commands),
                "outcome": outcome, "code": code, "signature": sign,
                "seconds": round(time.monotonic() - start, 2), "pdt": version(),
                "os": sys.platform, "python": platform.python_version()})
    except Exception:
        pass
    os.environ.pop(NOTE_ENV, None)
    if stop is not None:
        raise stop
    return 1 if failure is not None else result


def child(main: Callable[[], int]) -> int:
    try:
        result = main()
    except SystemExit as e:
        if exit_code(e.code) != 0:
            write_note(*handled(exit_code(e.code)))
        raise
    except Exception as e:
        write_note("unhandled", exception_signature(e))
        raise
    if exit_code(result) != 0:
        write_note(*handled(exit_code(result)))
    return result


def write_note(outcome: str, sign: str) -> None:
    try:
        path = Path(os.environ[NOTE_ENV])
        if path.is_file():
            kept = json.loads(path.read_text()).get("outcome")
            if NOTE_RANKS.get(kept, 0) >= NOTE_RANKS[outcome]:
                return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"outcome": outcome, "signature": sign}))
    except (OSError, KeyError, ValueError):
        pass


def version() -> str:
    from pdt import __version__
    return __version__


def record(entry: dict) -> None:
    now = datetime.now(timezone.utc)
    entry["interactive"] = can_offer()
    entry["offered"] = (entry["interactive"] and entry["outcome"] in ERRORS
                        and needs_help(recent(now) + [entry], now))
    HISTORY.parent.mkdir(parents=True, exist_ok=True)
    with HISTORY.open("a") as f:
        f.write(json.dumps(entry) + "\n")
    if entry["offered"]:
        offer(entry)


def recent(now: datetime) -> list[dict]:
    size = TAIL_BYTES
    while True:
        entries, whole = tail(size)
        if whole or not entries or not within(entries[0], now):
            return entries
        size *= 4


def tail(size: int) -> tuple[list[dict], bool]:
    if not HISTORY.is_file():
        return [], True
    with HISTORY.open("rb") as f:
        f.seek(0, os.SEEK_END)
        start = max(0, f.tell() - size)
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
    return entries, start == 0


def within(entry: dict, now: datetime) -> bool:
    try:
        then = datetime.strptime(entry["time"], TIME_FORMAT).replace(tzinfo=timezone.utc)
    except (KeyError, TypeError, ValueError):
        return False
    return now - then <= WINDOW


def needs_help(entries: list[dict], now: datetime) -> bool:
    if entries[-1].get("outcome") == "unhandled":
        return True
    errors = [entry for entry in entries
              if entry.get("interactive") is True and entry.get("outcome") in ERRORS
              and within(entry, now)]
    return len(errors) >= REPEATS


def command_type(command: str) -> str:
    word = next((word for word in command.split()[1:] if not is_flag(word)), "<arg>")
    return "pdt" if word == "<arg>" else f"pdt {word}"


def offer(entry: dict) -> None:
    from pdt import console
    console.say(f"Get help with {console.escape(command_type(entry['command']))} "
                f"at {console.value(SUPPORT_URL)}")
