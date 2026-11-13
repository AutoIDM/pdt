"""One place for terminal output.

Rich drops the colour when output is not a terminal, so a redirected or
piped run stays plain text and every command stays readable in a log.

The vocabulary is small on purpose. Bold marks a value the user may
type back or copy: a name, a command, a path, an id, an env var name, a
file name, a module name and version. Cyan marks a value
pdt worked out for the user to weigh: a numbered choice, the marker of a menu, a money
amount, the `==>` of a step. Dim marks text the user may skip: a
progress line, a side note. Colour otherwise marks state and nothing
else: red for a failure, green for a success, yellow for something the
user should read but need not act on. Everything else prints unstyled.

A message is rich markup. Wrap each value in value(), so it prints bold
inside a coloured or dim line, and pass text that pdt did not write (a
cloud's output, an error, a file's content) through escape().
tests/test_console_values.py fails on a value that is not wrapped.

Import the module, not its functions: `from pdt import console`, then
`console.error(...)`. The names here are short and would collide.
"""

from __future__ import annotations

import atexit
import os
import re
import sys
import threading
from contextlib import contextmanager
from functools import partial
from time import monotonic
from typing import Any, Callable, Iterator

from rich.cells import cell_len
from rich.console import Console
from rich.control import Control
from rich.markup import escape
from rich.segment import ControlType
from rich.text import Text

# Each line that a thread other than the main thread printed, in order, for the main
# thread to print.
_queued: list[partial] = []
_queued_lock = threading.Lock()
# True once the exit drain has run. No question can wait then, so every line prints at once.
_exited = False


class MainThreadConsole(Console):
    """A Console that queues each line printed from a thread other than the main thread, so
    the line never prints inside a question. The main thread prints the queued lines before
    its next line, after it reads an answer, and at exit."""

    def print(self, *objects: Any, **options: Any) -> None:
        main = threading.current_thread() is threading.main_thread()
        # A Control alone is a spinner's redraw from rich's refresh thread, not a line.
        if not main and not (objects and all(isinstance(o, Control) for o in objects)):
            with _queued_lock:
                if not _exited:
                    _queued.append(partial(super().print, *objects, **options))
                    return
        if main:
            drain()
        super().print(*objects, **options)

    def end_line(self) -> None:
        """End the current line, before any queued line prints."""
        super().print()

    def redraw(self, before: int, lines: list[Text]) -> None:
        """Print `lines` in the place of the `before` lines above the cursor, before any
        queued line prints. The old Windows console host moves up one line per code."""
        self.control(*[Control((ControlType.CURSOR_UP, 1))] * before)
        for line in lines + [Text()] * (before - len(lines)):
            self.control(Control((ControlType.ERASE_IN_LINE, 2)))
            super().print(line)
        self.control(*[Control((ControlType.CURSOR_UP, 1))] * (before - len(lines)))


def drain() -> None:
    """Print each queued line, in order. On another thread it prints nothing, because the
    main thread may be asking a question."""
    if threading.current_thread() is not threading.main_thread():
        return
    while True:
        with _queued_lock:
            if not _queued:
                return
            line = _queued.pop(0)
        line()


def drain_at_exit() -> None:
    """Print each queued line, and print each later line at once. An exit through os._exit
    skips atexit, so it calls this first."""
    global _exited
    while True:
        drain()
        with _queued_lock:
            if not _queued:
                _exited = True
                return


atexit.register(drain_at_exit)

# soft_wrap keeps a long path or command on one logical line: the terminal
# wraps it at the edge, and rich never re-flows it at a word boundary.
_console = MainThreadConsole(highlight=False, soft_wrap=True, emoji=False)
# For a line that goes to stderr, such as a warning or a wait line.
_errors = MainThreadConsole(stderr=True, highlight=False, soft_wrap=True, emoji=False)

# The real stdout, for `data`, once `json_output` has moved stdout to stderr.
_data = None

# Each message `warn_once` or `waiting` has printed in this process.
_warned: set[str] = set()

INDENT = 6


def width() -> int:
    return _console.width


def _wrap(line: Text, hang: int, room: int = 0) -> list[Text]:
    room = room or _console.width
    lines = []
    for number, paragraph in enumerate(line.split("\n", allow_blank=True)):
        if number:
            paragraph = Text(" " * hang) + paragraph
        values = [span for span in paragraph.spans if span.style == VALUE]
        words: list[tuple[int, int]] = []
        for match in re.finditer(r"\S+\s*", paragraph.plain):
            start, end = match.span()
            if end <= hang:
                continue
            if words and any(span.start < start < span.end for span in values):
                words[-1] = (words[-1][0], end)
            else:
                words.append((start, end))
        cuts, own_line = [], set()
        column = hang
        text_before = words != [] and paragraph.plain[:words[0][0]].strip() != ""
        for start, end in words:
            size = cell_len(paragraph.plain[start:end].rstrip())
            if size > room - hang:
                cuts += [end] if start in (0, *cuts[-1:]) else [start, end]
                own_line.add(start)
                column, text_before = hang, False
                continue
            if text_before and column + size > room:
                cuts.append(start)
                column = hang
            column += cell_len(paragraph.plain[start:end])
            text_before = True
        for offset, part in zip([0, *cuts], paragraph.divide(cuts)):
            if offset == 0 and cuts and not part.plain.strip():
                continue
            if offset == 0 or offset in own_line:
                lines.append(part)
            elif part.plain.strip():
                lines.append(Text(" " * hang) + part)
    for part in lines:
        part.rstrip()
    return lines


def _print(message: str | Text, hang: int | None = None, stderr: bool = False) -> None:
    out = _errors if stderr else _console
    if not out.is_terminal:
        out.print(message)
        return
    line = Text.from_markup(message, emoji=False) if isinstance(message, str) else message
    if hang is None:
        hang = len(line.plain) - len(line.plain.lstrip(" "))
    for part in _wrap(line, hang, out.width):
        out.print(part)


def to_stderr() -> None:
    """Print every line on stderr from now on, so stdout holds only what `data` prints."""
    _console.stderr = True


def json_output() -> None:
    """In a provider script started for --json output, point stdout at stderr.

    pdt.deploy sets PDT_JSON_OUTPUT for such a script. The move is at the file
    level, so a prompt, a print, and each program the script starts all reach
    stderr, and only `data` reaches stdout. Every provider script calls this first.
    """
    global _data
    if not os.environ.get("PDT_JSON_OUTPUT") or _data is not None:
        return
    _data = os.fdopen(os.dup(1), "w")
    os.dup2(2, 1)


def data(text: str) -> None:
    """Output for a program to read, such as a --json document."""
    drain()
    out = _data or sys.stdout
    out.write(text + "\n")
    out.flush()


def say(message: str = "", stderr: bool = False) -> None:
    """A line with no colour. `stderr` is for the error of a call that pdt tries again."""
    _print(message, stderr=stderr)


def art(text: str) -> None:
    """Text drawn in characters, such as pdt's banner. It is left out when the output's
    encoding, such as cp1252 on a redirected Windows stdout, has no code for one of them."""
    try:
        text.encode(_console.encoding)
    except UnicodeEncodeError:
        return
    _console.print(text, markup=False)


def progress(text: str) -> None:
    """A counter that ticks in place. Call say() to end the line."""
    if _console.is_terminal:
        line = Text(text)
        line.truncate(_console.width - 1)
        text = line.plain
    _console.print(text, markup=False, end="\r")


def styled(message: str) -> None:
    """A line the caller has already marked up."""
    _print(message)


def status(message: str) -> None:
    """A line about work in progress, such as a fetch or a login."""
    _print(f"[dim]{message}[/]")


class Wait:
    """What pdt waits for, and the seconds since it started waiting for it."""

    def __init__(self, message: str) -> None:
        self.now(message)

    def now(self, message: str) -> None:
        if getattr(self, "message", None) != message:
            self.message, self.start = message, monotonic()

    def __rich__(self) -> Text:
        line = Text.from_markup(f"[dim]{self.message} ({round(monotonic() - self.start)}s)[/]",
                                emoji=False, overflow="ellipsis")
        line.no_wrap = True
        return line


@contextmanager
def waiting(message: str, once: bool = False) -> Iterator[Wait]:
    """On a terminal that can animate, a spinner line under the output that names the wait and
    counts its seconds. Otherwise, or with JSON output, one plain line on stderr; with `once`, only the first time a process waits
    for `message`, as for a price read. `message` has the form `<action>: details`. A wait in
    a worker thread shows no spinner, since the thread that started it may show one."""
    wait = Wait(message)
    if _data is not None and not sys.stderr.isatty():
        yield wait
        return
    if not _console.is_interactive or _data is not None:
        if not once or message not in _warned:
            _warned.add(message)
            _print(f"[dim]{message}[/]", stderr=True)
        yield wait
        return
    if threading.current_thread() is not threading.main_thread():
        yield wait
        return
    with _console.status(wait):
        yield wait


class WaitOn:
    """One object pdt polls, its state at the last poll, and the seconds since the first
    poll that found it not ready. An empty `end` prints no end line."""

    def __init__(self, name: str, end: str) -> None:
        self.name, self.end, self.state, self.start, self.spinner = name, end, "", 0.0, None

    def now(self, state: str) -> None:
        if state == self.state:
            return
        if self.state == "":
            self.start = monotonic()
        self.state = state
        if not _console.is_interactive or _data is not None:
            _print(f"[dim]{state}: {self.name}[/]", stderr=True)
        elif self.spinner is None:
            self.spinner = _console.status(self)
            self.spinner.start()

    def __rich__(self) -> Text:
        line = Text.from_markup(
            f"[dim]{self.state}: {self.name} ({round(monotonic() - self.start)}s)[/]",
            emoji=False, overflow="ellipsis")
        line.no_wrap = True
        return line


@contextmanager
def wait_on(name: str, end: str, slow: str = "", result: bool = True) -> Iterator[WaitOn]:
    """One line for one object that pdt polls until it reaches a state, such as a queue
    that Batch deletes. `slow`, an `<action>: details` line, shows from the start in place
    of `name`, and no end line follows it. The end line of a wait that is not the command's
    `result`, such as a retried read, goes to stderr."""
    action, _, what = slow.partition(": ")
    wait = WaitOn(what or name, "" if slow else end)
    wait.now(action)
    try:
        yield wait
    finally:
        if wait.spinner is not None:
            wait.spinner.stop()
    if wait.state != "" and wait.end != "":
        _print(f"[dim]{wait.end}: {name} ({round(monotonic() - wait.start)}s)[/]",
               stderr=not result)


def field(label: str, value: str) -> None:
    """A label and the value pdt found for it: an account, a URL, a path."""
    _print(f"{escape(label)}: [{VALUE}]{escape(value)}[/]", len(label) + 2)


def choice(number: int, label: str, detail: str = "") -> None:
    """One numbered option the user picks with `number`. `detail` is the option's id or name,
    printed bold."""
    line = f"  [bold cyan]{number})[/] {escape(label)}"
    if detail != "":
        line += f"  [bold]{escape(detail)}[/]"
    _print(line, len(f"  {number}) "))


def error(message: str) -> None:
    _print(f"[bold red]error:[/] {message}", len("error: "))


def note(message: str) -> None:
    """Something pdt did not do, and why."""
    _print(f"[yellow]note:[/] {message}", len("note: "))


def warn(message: str) -> None:
    """A caution with no label, such as a re-prompt after a bad answer."""
    _print(f"[yellow]{message}[/]")


def warn_once(message: str) -> None:
    """A caution that a command may meet again and again, such as a failed read in each
    poll of `pdt logs --follow`: only its first time prints."""
    if message not in _warned:
        _warned.add(message)
        warn(message)


def step(message: str) -> None:
    """One reconcile action, printed as it happens."""
    _print(f"[bold cyan]==>[/] {message}", len("==> "))


def done(message: str) -> None:
    _print(f"[green]{message}[/]")


def failed(message: str) -> None:
    _print(f"[red]{message}[/]")


def event(text: str, mark: str = "") -> None:
    """One lifecycle event (pdt.lifecycle) as an `[alert]` line, bold magenta so it stands
    out in a run's output. `mark` is `not delivered to <type>:` when an alerts transport
    could not send the event."""
    prefix = f"{escape(mark)} " if mark else ""
    _print(f"[bold magenta]\\[alert] {prefix}{escape(text)}[/]", len("[alert] "))


def event_failed(message: str) -> None:
    """A lifecycle event that an alerts transport could not deliver, on stderr."""
    _print(f"[yellow]{message}[/]", stderr=True)


def heading(message: str) -> None:
    _print(f"[bold]{message}[/]")


def name(text: str) -> None:
    """A thing the user can type back to pdt: an app, an example."""
    _print(f"  [bold]{escape(text)}[/]")


class Markup(str):
    """Text that is already rich markup, such as a plan line. deploy.confirm escapes
    every plan line that is not one."""


VALUE = "bold not dim"


def value(text: object) -> str:
    """Markup that prints `text` bold. Inside dim text the value is bold and not dim,
    so it stands out."""
    return f"[{VALUE}]{escape(str(text))}[/]"


def detail(text: str, indent: int = INDENT) -> None:
    """Marked-up prose under a name, wrapped to the terminal and indented. Pass text
    that pdt did not write through escape(). A line
    breaks only at a space, so a name such as Import-Module stays whole. Spaces
    at the start of `text` indent every line of it."""
    if not _console.is_terminal:
        stripped = text.lstrip(" ")
        indent += len(text) - len(stripped)
        for part in Text.from_markup(stripped).wrap(_console, max(10, _console.width - indent)):
            part.rstrip()
            _console.print(Text(" " * indent) + part)
        return
    line = Text.from_markup(" " * indent + text, emoji=False)
    for part in _wrap(line, len(line.plain) - len(line.plain.lstrip(" "))):
        _console.print(part)


def bullet(text: str, indent: int = 2) -> None:
    """A marked-up line, indented. Pass text that pdt did not write through escape()."""
    _print(f"{' ' * indent}{text}", indent + 2)


def rows(heading: str, items: list[tuple[str, str]]) -> list[str]:
    """Lines for detail(): `heading` and each value bold with its marked-up note, on one
    line when there is one value, else one aligned row per value under the heading."""
    if len(items) == 1:
        return [f"{heading} {value(items[0][0])} ({items[0][1]})"]
    width = max(len(name) for name, _ in items)
    return [heading, *(f"  {value(name)}{' ' * (width - len(name))}  {note}"
                       for name, note in items)]


def command(text: str, note: str = "", indent: int = 2) -> None:
    """A command line or a path the user can copy, with an optional note."""
    line = f"{' ' * indent}[bold]{escape(text)}[/]"
    if note == "":
        _console.print(line)
    elif _console.is_terminal and indent + cell_len(text) + 2 + cell_len(note) > _console.width:
        _console.print(line)
        _print(f"{' ' * (indent + 4)}[dim]{escape(note)}[/]")
    else:
        _console.print(f"{line}  [dim]{escape(note)}[/]")


def next_steps(rows: list[tuple[str, str]], title: str = "Next steps:") -> None:
    """Commands the user may run next, each with what it does, under `title`."""
    heading(title)
    columns(rows)


def columns(rows: list[tuple[str, str]]) -> None:
    """Values the user may type or copy, such as commands or file names, each with a
    marked-up description, in two aligned columns: the value bold, the description dim.

    The second column starts after the longest value that leaves room for
    the descriptions. A longer value prints alone, and its description
    starts the next line in the second column. A description wraps to the
    terminal. On a terminal too narrow for two columns, every description
    prints under its value.
    """
    indent, gap = 2, 2
    notes = [Text.from_markup(what, style="dim") for _, what in rows]
    room = min(40, max(len(note) for note in notes))
    fits = [len(text) for text, _ in rows if indent + len(text) + gap + room <= _console.width]
    column = indent + max(fits) + gap if fits else INDENT
    for (text, _), note in zip(rows, notes):
        line = Text(" " * indent)
        line.append(text, style="bold")
        lines = list(note.wrap(_console, max(10, _console.width - column))) if note.plain else []
        for part in lines:
            part.rstrip()
        if lines and indent + len(text) + gap <= column:
            line.append(" " * (column - indent - len(text)))
            line.append(lines.pop(0))
        _console.print(line)
        for rest in lines:
            _console.print(Text(" " * column) + rest)


SECRET_CHANGE_COLOURS = {"deleted": "red", "new": "green", "updated": "yellow", "unchanged": "dim"}
RUN_STATUS_COLOURS = {"succeeded": "green", "ok": "green", "failed": "red", "unknown": "red",
                      "running": "cyan", "stopped": "yellow", "not yet run": "dim"}
LOG_LEVEL_COLOURS = {"DEBUG": "dim", "INFO": "green", "WARNING": "yellow", "ERROR": "bold red"}


def secret_changes(rows: list[tuple[str, str, str, str]]) -> None:
    """One row per env var: kind, name, the deployed value, the .env value (both masked)."""
    table(["", "env var", "deployed", ".env"], [list(row) for row in rows],
          row_styles=[[SECRET_CHANGE_COLOURS[row[0]], "bold"] for row in rows])


def log_line(time: str, level: str, message: str) -> None:
    """One line of a job's log: its time, its level coloured, then the message as is."""
    line = Text(f"{time:<8} ")
    line.append(f"{level:<7}", style=LOG_LEVEL_COLOURS.get(level, ""))
    line.append(f" {message}")
    line.rstrip()
    _print(line, len(f"{time:<8} {level:<7} "))


def _typed(password: bool) -> str:
    """One line that pdt reads key by key, and shows unless it is a password. A key that
    is no character, such as an arrow key, shows nothing and is not in the line. Backspace
    removes one character, Ctrl+W removes one word, and Ctrl+U removes them all, as in the
    line mode of the terminal."""
    out = _console.file
    line: list[str] = []
    with keys() as key:
        while True:
            pressed = key()
            if pressed == "space":
                pressed = " "
            if pressed == "enter":
                break
            if pressed == "eof" and not line:
                raise EOFError
            erased = ""
            if pressed == "backspace" and line:
                erased = line.pop()
            elif pressed == "clear":
                erased = "".join(line)
                line.clear()
            elif pressed == "word":
                kept = re.sub(r"\S*\s*$", "", "".join(line))
                erased = "".join(line[len(kept):])
                del line[len(kept):]
            elif len(pressed) == 1:
                line.append(pressed)
                if not password:
                    out.write(pressed)
            if not password:
                out.write("\b \b" * cell_len(erased))
            out.flush()
    out.write("\n")
    out.flush()
    return "".join(line)


def _answer(prompt: Text, password: bool = False) -> str:
    """Read one answer, then print each line that another thread printed meanwhile. On Ctrl+C
    or end of input, or when stdin is not a terminal and so shows no answer, end the
    question line first, because input() and getpass print no newline then. On macOS and
    Linux the terminal itself shows the bytes of an arrow key and puts them in the line, so
    there pdt reads the line of a terminal with _typed. The Windows console edits the line."""
    if _console.is_terminal:
        prompt = Text("\n").join(_wrap(prompt, 0, _console.width - 1))
        prompt.append(" ")
    try:
        if sys.platform != "win32" and _at_terminal():
            _console.print(prompt, end="")
            return _typed(password)
        answer = _console.input(prompt, password=password)
        if sys.stdin is not None and not sys.stdin.isatty():
            _console.end_line()
        return answer
    except (EOFError, KeyboardInterrupt):
        _console.end_line()
        raise
    finally:
        drain()


def ask(question: str, default: str = "") -> str:
    suffix = f" [{default}]" if default != "" else ""
    return _answer(Text(f"{question}{suffix}: ")).strip()


def read(prompt: str) -> str:
    """The line the user types after `prompt`, which prints as is."""
    return _answer(Text(prompt)).strip()


def secret(prompt: str) -> str:
    """The line the user types after `prompt`, which does not show as it is typed."""
    return _answer(Text(prompt), password=True).strip()


YES_NO = {"y": True, "yes": True, "n": False, "no": False}


def confirm(question: str = "Proceed?", default: bool = False, choices: str = "") -> bool:
    """Ask until the answer is y, yes, n, or no in any case; Enter takes the default.
    `choices` replaces the [y/N] after the question.

    End of input raises EOFError."""
    choices = escape(choices) or ("\\[Y/n]" if default else "\\[y/N]")
    while True:
        answer = _answer(Text.from_markup(f"[bold]{escape(question)}[/] {choices} ",
                                          emoji=False)).strip().lower()
        if answer == "":
            return default
        if answer in YES_NO:
            return YES_NO[answer]
        _print("Please answer y or n.")


def _at_terminal() -> bool:
    """True when the input and the output are a terminal. rich also reports a terminal for
    output in a pipe when FORCE_COLOR or TTY_COMPATIBLE is set, so ask the file too."""
    return (sys.stdin is not None and sys.stdin.isatty() and _console.is_terminal
            and _console.file.isatty())


def can_menu() -> bool:
    """True when a question can show a menu the user moves through with the arrow keys:
    the input is a terminal, and the output is a terminal at least 20 columns wide that can
    move its cursor. Otherwise the question prints numbered lines and reads a typed answer."""
    return _at_terminal() and not _console.is_dumb_terminal and _console.width >= 20


POSIX_KEYS = {b"\r": "enter", b"\n": "enter", b" ": "space", b"\x1b[A": "up", b"\x1bOA": "up",
              b"\x1b[B": "down", b"\x1bOB": "down", b"\x7f": "backspace", b"\x08": "backspace",
              b"\x15": "clear", b"\x17": "word", b"\x04": "eof"}
# The keys of the Windows console by virtual-key code: VK_UP, VK_DOWN, VK_RETURN, VK_SPACE,
# VK_BACK.
WINDOWS_KEYS = {0x26: "up", 0x28: "down", 0x0d: "enter", 0x20: "space", 0x08: "backspace"}
# The seconds to wait for the byte after Esc. The Esc key alone sends Esc and no more.
ESC_WAIT = 0.05
# The seconds to wait for each later byte of a sequence after Esc and `[` or `O`, which a
# slow SSH connection can bring in a separate read.
SEQUENCE_WAIT = 0.5


# The bytes that _posix_key read after the end of one key. They start the next key.
_unread = b""


def _next_byte(fd: int, wait: float | None) -> bytes:
    """The next byte of the input, or b"" at its end. With `wait` in seconds it is also b""
    when no byte comes in that time."""
    global _unread
    if _unread:
        byte, _unread = _unread[:1], _unread[1:]
        return byte
    if wait is not None:
        import select
        if not select.select([fd], [], [], wait)[0]:
            return b""
    return os.read(fd, 1)


def _escape_sequence(fd: int) -> bytes:
    """The bytes of one key that starts with Esc. Esc and `[` start a sequence of bytes
    0x30-0x3f, then bytes 0x20-0x2f, then one last byte 0x40-0x7e. Esc and `O` have one last
    byte. Esc and another byte is that key with Alt. A byte that cannot go on a sequence
    ends it and starts the next key."""
    global _unread
    key = b"\x1b" + _next_byte(fd, ESC_WAIT)
    if key not in (b"\x1b[", b"\x1bO"):
        return key
    byte = _next_byte(fd, SEQUENCE_WAIT)
    if key == b"\x1b[":
        for low, high in ((0x30, 0x3f), (0x20, 0x2f)):
            while byte and low <= byte[0] <= high:
                key += byte
                byte = _next_byte(fd, SEQUENCE_WAIT)
        if key == b"\x1b[" and byte == b"[":
            # A function key of the Linux console: Esc, `[`, `[`, and one letter.
            key += byte
            byte = _next_byte(fd, SEQUENCE_WAIT)
    if byte and 0x40 <= byte[0] <= 0x7e:
        return key + byte
    _unread = byte + _unread
    return key


def _posix_key(fd: int) -> str:
    global _unread
    key = _next_byte(fd, None)
    if key == b"":
        raise EOFError
    if key == b"\x1b":
        key = _escape_sequence(fd)
        if key == b"\x1b\x1b":
            # Alt with a key that sends its own sequence, such as Alt+Up in iTerm2.
            _escape_sequence(fd)
    elif 0xc2 <= key[0] <= 0xf4:
        # The first byte of a UTF-8 character tells how many bytes follow it. No character
        # starts with another byte from 0x80 up.
        for _ in range(1 if key[0] < 0xe0 else 2 if key[0] < 0xf0 else 3):
            byte = _next_byte(fd, None)
            if not (byte and 0x80 <= byte[0] <= 0xbf):
                _unread = byte + _unread
                return ""
            key += byte
    if key in POSIX_KEYS:
        return POSIX_KEYS[key]
    if key[0] < 0x20:
        return ""
    return key.decode(errors="ignore")


def _console_event(kernel32, handle, record) -> tuple[bool, int, str, int]:
    """The next input event of the Windows console, read into `record`: whether it is a key
    press, its virtual-key code, its character ("\\0" when it has none), and how many times
    a held key gave it. An arrow key is one event with its own code, so it never looks like
    a typed character such as `à`."""
    import ctypes
    from ctypes import wintypes
    count = wintypes.DWORD()
    if not kernel32.ReadConsoleInputW(handle, ctypes.byref(record), 1,
                                      ctypes.byref(count)) or count.value == 0:
        raise EOFError
    # KEY_EVENT is 1.
    down = record.type == 1 and bool(record.event.down)
    return down, record.event.code, record.event.char, record.event.repeat


def _windows_keys(kernel32, handle, record) -> Iterator[str]:
    while True:
        down, code, char, repeat = _console_event(kernel32, handle, record)
        if not down:
            continue
        if code in WINDOWS_KEYS:
            key = WINDOWS_KEYS[code]
        elif char == "\x03":
            raise KeyboardInterrupt
        elif char == "\x1a":
            key = "eof"
        elif char != "\0":
            key = char if char >= " " else ""
        else:
            # A key with no character, such as Shift or Left, is no key.
            continue
        for _ in range(repeat):
            yield key


@contextmanager
def keys() -> Iterator[Callable[[], str]]:
    """A function that waits for one key and returns its name: `up`, `down`, `space`,
    `enter`, `backspace`, `clear` (Ctrl+U), `word` (Ctrl+W), or `eof` (Ctrl+D, or Ctrl+Z on
    Windows). Another key gives its one character, or "" when it has none. The function
    raises EOFError when the input ends, and Ctrl+C raises KeyboardInterrupt.

    On macOS and Linux, Ctrl+Z gives the shell the terminal as pdt found it, with the cursor
    shown, and after `fg` the function returns `resumed`. A parent that ignores Ctrl+Z, or
    that set its own handler, keeps it."""
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class KeyEvent(ctypes.Structure):
            _fields_ = [("down", wintypes.BOOL), ("repeat", wintypes.WORD),
                        ("code", wintypes.WORD), ("scan", wintypes.WORD),
                        ("char", wintypes.WCHAR), ("state", wintypes.DWORD)]

        class Record(ctypes.Structure):
            # KEY_EVENT_RECORD is the largest member of the event union, so it gives its size.
            _fields_ = [("type", wintypes.WORD), ("event", KeyEvent)]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetStdHandle.argtypes = (wintypes.DWORD,)
        kernel32.GetStdHandle.restype = wintypes.HANDLE
        kernel32.GetConsoleMode.argtypes = (wintypes.HANDLE, wintypes.LPDWORD)
        kernel32.GetConsoleMode.restype = wintypes.BOOL
        kernel32.SetConsoleMode.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel32.SetConsoleMode.restype = wintypes.BOOL
        kernel32.ReadConsoleInputW.argtypes = (wintypes.HANDLE, ctypes.POINTER(Record),
                                               wintypes.DWORD, wintypes.LPDWORD)
        kernel32.ReadConsoleInputW.restype = wintypes.BOOL
        # STD_INPUT_HANDLE is -10.
        handle = kernel32.GetStdHandle(-10)
        mode = wintypes.DWORD()
        # The console mode is 0 for the whole menu, as in msvcrt.getwch, so Ctrl+C is the
        # character "\x03" and an arrow key is one event even when the parent turned on
        # ENABLE_VIRTUAL_TERMINAL_INPUT. Input that is not a console keeps its mode.
        console_input = kernel32.GetConsoleMode(handle, ctypes.byref(mode))
        try:
            if console_input:
                kernel32.SetConsoleMode(handle, 0)
            windows_keys = _windows_keys(kernel32, handle, Record())
            yield lambda: next(windows_keys)
        finally:
            if console_input:
                kernel32.SetConsoleMode(handle, mode.value)
        return
    import select
    import signal
    import termios
    import tty
    global _unread
    _unread = b""
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    # A pipe that gets one byte after `fg`, so that a wait for a key ends then.
    resume_read, resume_write = os.pipe()

    def stop(signum, frame):
        # The signal can come while the main thread writes to the output, so this writes
        # to the file descriptor and draws nothing.
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        os.write(_console.file.fileno(), b"\x1b[?25h")
        signal.signal(signal.SIGTSTP, signal.SIG_DFL)
        os.kill(os.getpid(), signal.SIGTSTP)
        # The process stops at the line above, and goes on from here after `fg`.
        signal.signal(signal.SIGTSTP, stop)
        tty.setcbreak(fd, termios.TCSANOW)
        os.write(resume_write, b"r")

    def key() -> str:
        if not _unread and resume_read in select.select([fd, resume_read], [], [])[0]:
            # Each `fg` since the last key left one byte, and one new copy of the menu is
            # enough for all of them.
            os.read(resume_read, 1024)
            return "resumed"
        return _posix_key(fd)

    main = threading.current_thread() is threading.main_thread()
    handled = main and signal.getsignal(signal.SIGTSTP) == signal.SIG_DFL
    try:
        if handled:
            signal.signal(signal.SIGTSTP, stop)
        try:
            tty.setcbreak(fd, termios.TCSANOW)
            yield key
        finally:
            # The handler goes first, so that a Ctrl+Z from here on cannot set cbreak again
            # after the saved mode.
            if handled:
                signal.signal(signal.SIGTSTP, signal.SIG_DFL)
            termios.tcsetattr(fd, termios.TCSADRAIN, saved)
    finally:
        os.close(resume_read)
        os.close(resume_write)


def _menu_line(marker: str, label: str, detail: str = "") -> Text:
    line = Text()
    line.append(marker, style="bold cyan")
    if detail != "":
        # The detail tells two lines apart, so the label gives up its room first.
        room = _console.width - 1 - len(marker) - len(detail) - 2
        if len(label) > room:
            label = label[:max(room - 1, 0)] + "…" if room > 0 else ""
    line.append(label)
    if detail != "":
        line.append(f"  {detail}", style="bold")
    line.truncate(_console.width - 1, overflow="ellipsis")
    return line


MENU_LINES = 10
MENU_WRAP = 4


def _menu(labels: list[str], details: list[str], on: set[int] | None, at: int) -> int:
    """Show one line per label and a marker the Up and Down keys move, until Enter. With
    `on`, each line has a box and Space turns it on or off. A list longer than MENU_LINES
    or the screen shows the part around the marker. In a list longer than MENU_WRAP lines,
    the marker goes from one end to the other. Then the lines of the answer take the menu's
    place."""
    arrows = "↑ ↓"
    try:
        arrows.encode(_console.encoding)
    except UnicodeEncodeError:
        arrows = "Up and Down"
    room = min(MENU_LINES, max(3, _console.height - 4))
    wrap = len(labels) > MENU_WRAP
    top = drawn = 0

    def draw() -> None:
        nonlocal top, drawn
        top = min(max(top, at - room + 1), at)
        shown = range(top, min(top + room, len(labels)))
        lines = []
        if on is not None:
            lines.append(Text(f"  {arrows} move, Space turns on or off, Enter goes on",
                              style="dim"))
        if top > 0:
            lines.append(Text(f"  {top} more above", style="dim"))
        for index in shown:
            box = "" if on is None else "[x] " if index in on else "[ ] "
            lines.append(_menu_line("> " if index == at else "  ", box + labels[index],
                                    details[index]))
        if shown.stop < len(labels):
            lines.append(Text(f"  {len(labels) - shown.stop} more below", style="dim"))
        for line in lines:
            # A line that wraps takes two rows, and redraw counts one row for each line.
            line.truncate(_console.width - 1, overflow="ellipsis")
        _console.redraw(drawn, lines)
        drawn = len(lines)

    _console.show_cursor(False)
    try:
        with keys() as key:
            while True:
                draw()
                pressed = key()
                if pressed == "resumed":
                    # After `fg`, the shell's lines are under the menu, so draw a new menu
                    # under them.
                    drawn = 0
                    _console.show_cursor(False)
                elif pressed == "up" and wrap and at == 0:
                    at = len(labels) - 1
                elif pressed == "up":
                    at = max(0, at - 1)
                elif pressed == "down" and wrap and at == len(labels) - 1:
                    at = 0
                elif pressed == "down":
                    at = min(len(labels) - 1, at + 1)
                elif pressed == "space" and on is not None:
                    on ^= {at}
                elif pressed == "enter":
                    break
                elif pressed == "eof":
                    raise EOFError
        if on is None:
            answer = [_menu_line("> ", labels[at], details[at])]
        else:
            answer = [_menu_line("  ", f"[x] {labels[index]}") for index in sorted(on)]
        _console.redraw(drawn, answer or [Text("  none", style="dim")])
    finally:
        _console.show_cursor(True)
        drain()
    return at


def choose_one(labels: list[str], default: int = 0, details: list[str] | None = None) -> int:
    """The index of the label the user chooses in a menu. The marker starts on `default`.
    `details` holds each label's id or name, printed bold. Call it only when can_menu()."""
    return _menu(labels, details or [""] * len(labels), None, default)


def choose_many(labels: list[str], on: list[bool]) -> list[bool]:
    """For each label, whether the user leaves it on in a menu of boxes. `on` holds the
    state of each box at the start. Call it only when can_menu()."""
    marked = {index for index, mark in enumerate(on) if mark}
    _menu(labels, [""] * len(labels), marked, 0)
    return [index in marked for index in range(len(labels))]


def _row(cells: list[str], widths: list[int], styles: list[str]) -> Text:
    line = Text()
    for index, cell in enumerate(cells):
        style = styles[index] if index < len(styles) else ""
        line.append(cell, style=style or "")
        if index < len(cells) - 1:
            line.append(" " * (widths[index] - len(cell) + 2))
    line.rstrip()
    return line


# A symbol that names one currency stands alone. A symbol that several
# currencies share is followed by the code on the total line, so "$" never
# hides which dollar.
CURRENCY_SYMBOLS = {
    "GBP": "£", "EUR": "€", "INR": "₹", "KRW": "₩", "ILS": "₪", "TRY": "₺", "PHP": "₱",
    "VND": "₫", "UAH": "₴", "NGN": "₦", "THB": "฿", "PLN": "zł", "BRL": "R$",
}
SHARED_SYMBOLS = {
    **dict.fromkeys(("USD", "CAD", "AUD", "NZD", "SGD", "HKD", "TWD", "MXN", "ARS", "CLP",
                     "COP"), "$"),
    **dict.fromkeys(("JPY", "CNY"), "¥"),
    **dict.fromkeys(("SEK", "NOK", "DKK", "ISK"), "kr"),
    "ZAR": "R",
}


def currency_prefix(currency: str) -> str:
    """`£`, `$USD`, or `CHF ` before an amount."""
    if currency in CURRENCY_SYMBOLS:
        return CURRENCY_SYMBOLS[currency]
    if currency in SHARED_SYMBOLS:
        return SHARED_SYMBOLS[currency] + currency
    return f"{currency} "


def size(count: int) -> str:
    if count < 1024:
        return f"{count} B"
    amount = count / 1024
    for unit in ("KB", "MB", "GB"):
        if amount < 1024:
            return f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{amount:.1f} TB"


def cost(items: list[tuple[str, float]], prices: str, excludes: str = "",
         currency: str = "USD") -> None:
    """A monthly cost estimate: one line per item, then the total."""
    width = max(len(label) for label, _ in items)
    total_symbol = currency_prefix(currency)
    symbol = escape(SHARED_SYMBOLS.get(currency, total_symbol).ljust(len(total_symbol)))
    room = _console.width - len(f"    {total_symbol}{0:>7.2f}")
    if _console.is_terminal and width > room:
        width = max(10, room)
    _print(f"[bold]Estimated monthly cost[/] ({escape(prices)}):")
    for label, amount in items:
        first, *rest = _wrap(Text(label), 2, width) if len(label) > width else [Text(label)]
        _console.print(f"  {escape(first.plain):<{width}}  [cyan]{symbol}{amount:>7.2f}[/]")
        for part in rest:
            _console.print(Text("  ") + part)
    total = sum(amount for _, amount in items)
    _console.print(
        f"  [bold]{'total':<{width}}  [cyan]{escape(total_symbol)}{total:>7.2f}[/][/]")
    if excludes != "":
        _print(f"  [dim]{escape(excludes)}[/]")


def table(headers: list[str], rows: list[list[str]],
          styles: list[str] | None = None,
          row_styles: list[list[str]] | None = None) -> None:
    """Aligned columns. Cell text is literal, and no line ends in a space.

    `styles` holds one style per column. `row_styles`, when given, holds one
    such list per row instead, so a cell can be coloured by its value.
    """
    cells = [[str(cell) for cell in row] for row in rows]
    widths = [max(len(row[index]) for row in (headers, *cells))
              for index in range(len(headers))]
    if _console.is_terminal and max(_row(row, widths, []).cell_len
                                    for row in (headers, *cells)) > _console.width:
        _stacked(headers, cells, row_styles or [styles or []] * len(cells))
        return
    _console.print(_row(headers, widths, ["bold"] * len(headers)))
    for index, row in enumerate(cells):
        _console.print(_row(row, widths, row_styles[index] if row_styles else styles or []))


def _stacked(headers: list[str], rows: list[list[str]], row_styles: list[list[str]]) -> None:
    for row, styles in zip(rows, row_styles):
        styles = styles + [""] * (len(row) - len(styles))
        _console.print(Text(row[0], style=f"bold {styles[0]}".strip()))
        line = Text("  ")
        for header, cell, style in zip(headers[1:], row[1:], styles[1:]):
            if cell == "":
                continue
            field = Text(f"{header}: " if header else "")
            field.append(cell, style=style)
            if line.plain.strip() and line.cell_len + 2 + field.cell_len > _console.width:
                _console.print(line)
                line = Text("  ")
            line.append_text(Text("  ") + field if line.plain.strip() else field)
        if line.plain.strip():
            _console.print(line)
