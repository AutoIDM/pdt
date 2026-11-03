"""Keep a history of each top-level pdt command, and point a person at the pdt
community after a pdt command fails at a terminal.

The history in `HISTORY` holds only pdt's own words (its command and flag names, the text
in its code, and exception names); every other word becomes a placeholder. `pdt support`
reads it.
When pdt itself crashes at a terminal, `pdt support` starts on its own. It sends
nothing anywhere.
"""

from __future__ import annotations

import ast
import builtins
import functools
import hashlib
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

_seen = ("", "")

VALUE, ARG, NUMBER = "<value>", "<arg>", "#"
EDGES = ".,:;!?()[]{}'\"`"


def saw(message: str, usage: bool = False) -> None:
    global _seen
    _seen = ("usage" if usage else "", message)


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
            words.append(ARG)
    return " ".join(words)


def hide(text: str, values: set[str], placeholder: str, inside: str,
         shortest: int = 4, flags: int = 0) -> str:
    """`text` with each value replaced: one of `shortest` characters or more wherever it is,
    a shorter one only where no `inside` character touches it. A short number stays, because
    signature makes each number NUMBER."""
    for value in sorted(values, key=len, reverse=True):
        if value == "" or (len(value) < shortest and value.isdigit()):
            continue
        pattern = re.escape(value)
        if len(value) < shortest:
            pattern = rf"(?<!{inside}){pattern}(?!{inside})"
        text = re.sub(pattern, lambda _match: placeholder, text, flags=flags)
    return text


WORD_PART = rf"[^\s{re.escape(EDGES)}]"


@functools.cache
def known_words() -> frozenset[str]:
    """The words of pdt's own text, the string constants in its package, the places in it
    that exception_signature names, and the names of Python's exceptions."""
    words = {VALUE, ARG, NUMBER}
    words.update(name for name, value in vars(builtins).items()
                 if isinstance(value, type) and issubclass(value, BaseException))
    for path in PACKAGE.rglob("*.py"):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError, UnicodeError, ValueError):
            continue
        file = path.relative_to(PACKAGE).as_posix()
        words.add(f"{file}:<module>")
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                words.add(f"{file}:{node.name}")
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                words.update(word.strip(EDGES) for word in node.value.split())
    return frozenset(words)


def signature(text: str, typed: list[str] = ()) -> str:
    """The first line of `text` with only pdt's own words: a word the user typed becomes
    ARG, a number NUMBER, and every other word VALUE."""
    from rich.errors import MarkupError
    from rich.text import Text
    try:
        text = Text.from_markup(text).plain
    except MarkupError:
        pass
    line = next((line.strip() for line in text.splitlines() if line.strip()), "")
    line = hide(line, set(os.environ.values()), VALUE, WORD_PART)
    line = hide(line, set(typed), ARG, WORD_PART)

    def public(match: re.Match) -> str:
        token = match.group()
        word = token.strip(EDGES)
        if word == "":
            return token
        if word.isdigit():
            kept = NUMBER
        elif word in known_words():
            kept = word
        else:
            kept = VALUE
        start = token.index(word)
        return token[:start] + kept + token[start + len(word):]
    return re.sub(r"\S+", public, line)[:160]


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


def typed_words(argv: list[str], commands: set[str], flags: set[str] = frozenset()) -> list[str]:
    words = []
    command = command_index(argv, commands)
    for index, token in enumerate(argv[1:], 1):
        flag, _, value = token.partition("=")
        if flag not in flags and index != command:
            words.append(token)
        words.append(value)
    return [word for word in words if word != ""]


def private_names() -> set[str]:
    """Names that can be one of pdt's own words and still name the user: the home folder,
    the computer, the project, its apps, and their platform settings except the provider."""
    names = {Path.home().name, platform.node(), platform.node().split(".")[0]}
    try:
        project = config.find_project()
        names |= {project.name, *config.app_folders()}
        settings = config.load_yaml(project / config.PROJECT_FILE)
        if isinstance(settings.get("platform"), dict):
            names |= {str(value) for key, value in settings["platform"].items()
                      if key != "provider"}
        entries = settings.get("apps")
        if isinstance(entries, list):
            names |= {str(entry["name"]) for entry in entries
                      if isinstance(entry, dict) and "name" in entry}
        for app in config.app_folders():
            try:
                names |= {str(value) for key, value in config.merged_app(app)["platform"].items()
                          if key != "provider"}
            except config.ConfigError:
                continue
    except (config.ConfigError, OSError, UnicodeError):
        pass
    return {name for name in names if len(name) > 1}


def without_names(text: str, names: set[str]) -> str:
    return hide(text, names, "<app>", "[A-Za-z0-9]", shortest=sys.maxsize, flags=re.IGNORECASE)


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
        typed = typed_words(sys.argv, commands, flags)
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
                sign = hide(str(child_note["signature"]), set(typed), ARG, WORD_PART)
                if child_note.get("outcome") == "unhandled":
                    outcome = "unhandled"
        if code != 0:
            sign = without_names(sign, private_names())
        entry = {"v": 1, "time": datetime.now(timezone.utc).strftime(TIME_FORMAT),
                 "project": project(), "command": shape(sys.argv, commands, flags),
                 "outcome": outcome, "code": code, "signature": sign,
                 "seconds": round(time.monotonic() - start, 2), "pdt": version(),
                 "os": sys.platform, "python": platform.python_version()}
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
        write_note(lambda error=e: ("unhandled", exception_signature(error)))
        raise
    if exit_code(result) != 0:
        write_note(lambda: handled(exit_code(result)))
    return result


def write_note(make: Callable[[], tuple[str, str]]) -> None:
    """Write the outcome and signature that `make` gives to the parent's note. Nothing that
    goes wrong here changes the child's exit code or exception."""
    try:
        outcome, sign = make()
        path = Path(os.environ[NOTE_ENV])
        if path.is_file():
            kept = json.loads(path.read_text()).get("outcome")
            if NOTE_RANKS.get(kept, 0) >= NOTE_RANKS[outcome]:
                return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"outcome": outcome, "signature": sign}))
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
