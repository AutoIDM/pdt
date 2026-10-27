"""One place for terminal output.

Rich drops the colour when output is not a terminal, so a redirected or
piped run stays plain text and every command stays readable in a log.

The vocabulary is small on purpose. Bold marks a value the user may
type back or copy: a name, a command, a path, an id, an env var name, a
file name, a module name and version. Cyan marks a value
pdt worked out for the user to weigh: a numbered choice, a money
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
import sys
import threading
from contextlib import contextmanager
from functools import partial
from time import monotonic
from typing import Any, Iterator

from rich.console import Console
from rich.control import Control
from rich.markup import escape
from rich.text import Text

# Each line that a thread other than the main thread printed, in order, for the main
# thread to print.
_queued: list[partial] = []
_queue = threading.Lock()


class MainThread(Console):
    """A Console on which only the main thread writes, so a line from another thread, such
    as an alert that could not send, never lands inside a question. Such a line waits in a
    queue until the main thread prints its next line, reads an answer, or exits."""

    def print(self, *objects: Any, **options: Any) -> None:
        main = threading.current_thread() is threading.main_thread()
        # A Control alone is a spinner's redraw from rich's refresh thread, not a line.
        if not main and not (objects and all(isinstance(o, Control) for o in objects)):
            with _queue:
                _queued.append(partial(super().print, *objects, **options))
            return
        if main:
            drain()
        super().print(*objects, **options)


def drain() -> None:
    """Print each queued line, in order. Only the main thread calls this."""
    while True:
        with _queue:
            if not _queued:
                return
            line = _queued.pop(0)
        line()


atexit.register(drain)

# soft_wrap keeps a long path or command on one logical line: the terminal
# wraps it at the edge, and rich never re-flows it at a word boundary.
_console = MainThread(highlight=False, soft_wrap=True, emoji=False)
# For a line from another thread that must reach stderr while _console prints on.
_errors = MainThread(stderr=True, highlight=False, soft_wrap=True, emoji=False)

# The real stdout, for `data`, once `json_output` has moved stdout to stderr.
_data = None

# Each message `warn_once` or `waiting` has printed in this process.
_warned: set[str] = set()

INDENT = 6


def width() -> int:
    return _console.width


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
    out = _data or sys.stdout
    out.write(text + "\n")
    out.flush()


def say(message: str = "", stderr: bool = False) -> None:
    """A line with no colour. `stderr` is for the error of a call that pdt tries again."""
    (_errors if stderr else _console).print(message)


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
    _console.print(text, markup=False, end="\r")


def styled(message: str) -> None:
    """A line the caller has already marked up."""
    _console.print(message)


def status(message: str) -> None:
    """A line about work in progress, such as a fetch or a login."""
    _console.print(f"[dim]{message}[/]")


class Wait:
    """What pdt waits for, and the seconds since it started waiting for it."""

    def __init__(self, message: str) -> None:
        self.now(message)

    def now(self, message: str) -> None:
        if getattr(self, "message", None) != message:
            self.message, self.start = message, monotonic()

    def __rich__(self) -> str:
        return f"[dim]{self.message} ({round(monotonic() - self.start)}s)[/]"


@contextmanager
def waiting(message: str, once: bool = False) -> Iterator[Wait]:
    """On a terminal that can animate, a spinner line under the output that names the wait and
    counts its seconds. Otherwise, or with JSON output, one plain line on stderr; with `once`, only the first time a process waits
    for `message`, as for a price read. `message` has the form `<action>: details`. A wait in
    a worker thread shows no spinner, since the thread that started it may show one."""
    wait = Wait(message)
    if not _console.is_interactive or _data is not None:
        if not once or message not in _warned:
            _warned.add(message)
            _errors.print(f"[dim]{message}[/]")
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
            _errors.print(f"[dim]{state}: {self.name}[/]")
        elif self.spinner is None:
            self.spinner = _console.status(self)
            self.spinner.start()

    def __rich__(self) -> str:
        return f"[dim]{self.state}: {self.name} ({round(monotonic() - self.start)}s)[/]"


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
        (_console if result else _errors).print(
            f"[dim]{wait.end}: {name} ({round(monotonic() - wait.start)}s)[/]")


def field(label: str, value: str) -> None:
    """A label and the value pdt found for it: an account, a URL, a path."""
    _console.print(f"{escape(label)}: [bold]{escape(value)}[/]")


def choice(number: int, label: str, detail: str = "") -> None:
    """One numbered option the user picks with `number`. `detail` is the option's id or name,
    printed bold."""
    line = f"  [bold cyan]{number})[/] {escape(label)}"
    if detail != "":
        line += f"  [bold]{escape(detail)}[/]"
    _console.print(line)


def error(message: str) -> None:
    _console.print(f"[bold red]error:[/] {message}")


def note(message: str) -> None:
    """Something pdt did not do, and why."""
    _console.print(f"[yellow]note:[/] {message}")


def warn(message: str) -> None:
    """A caution with no label, such as a re-prompt after a bad answer."""
    _console.print(f"[yellow]{message}[/]")


def warn_once(message: str) -> None:
    """A caution that a command may meet again and again, such as a failed read in each
    poll of `pdt logs --follow`: only its first time prints."""
    if message not in _warned:
        _warned.add(message)
        warn(message)


def step(message: str) -> None:
    """One reconcile action, printed as it happens."""
    _console.print(f"[bold cyan]==>[/] {message}")


def done(message: str) -> None:
    _console.print(f"[green]{message}[/]")


def failed(message: str) -> None:
    _console.print(f"[red]{message}[/]")


def event(text: str, mark: str = "") -> None:
    """One lifecycle event (pdt.lifecycle) as an `[alert]` line, bold magenta so it stands
    out in a run's output. `mark` is `not delivered to <type>:` when an alerts transport
    could not send the event."""
    prefix = f"{escape(mark)} " if mark else ""
    _console.print(f"[bold magenta]\\[alert] {prefix}{escape(text)}[/]")


def event_failed(message: str) -> None:
    """A lifecycle event that an alerts transport could not deliver, on stderr."""
    _errors.print(f"[yellow]{message}[/]")


def heading(message: str) -> None:
    _console.print(f"[bold]{message}[/]")


def name(text: str) -> None:
    """A thing the user can type back to pdt: an app, an example."""
    _console.print(f"  [bold]{escape(text)}[/]")


class Markup(str):
    """Text that is already rich markup, such as a plan line. deploy.confirm escapes
    every plan line that is not one."""


def value(text: object) -> str:
    """Markup that prints `text` bold. Inside dim text the value is bold and not dim,
    so it stands out."""
    return f"[bold not dim]{escape(str(text))}[/]"


def detail(text: str, indent: int = INDENT) -> None:
    """Marked-up prose under a name, wrapped to the terminal and indented. Pass text
    that pdt did not write through escape(). A line
    breaks only at a space, so a name such as Import-Module stays whole. Spaces
    at the start of `text` indent every line of it."""
    stripped = text.lstrip(" ")
    indent += len(text) - len(stripped)
    for part in Text.from_markup(stripped).wrap(_console, max(10, _console.width - indent)):
        part.rstrip()
        _console.print(Text(" " * indent) + part)


def bullet(text: str, indent: int = 2) -> None:
    """A marked-up line, indented. Pass text that pdt did not write through escape()."""
    _console.print(f"{' ' * indent}{text}")


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
    if note != "":
        _console.print(f"{' ' * indent}[bold]{escape(text)}[/]  [dim]{escape(note)}[/]")
    else:
        _console.print(f"{' ' * indent}[bold]{escape(text)}[/]")


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
                      "running": "cyan", "not yet run": "dim"}
LOG_LEVEL_COLOURS = {"DEBUG": "dim", "INFO": "green", "WARNING": "yellow", "ERROR": "bold red"}


def secret_changes(rows: list[tuple[str, str, str, str]]) -> None:
    """One row per env var: kind, name, the deployed value, the .env value (both masked)."""
    headers = ["", "env var", "deployed", ".env"]
    widths = [max(len(cell) for cell in column) for column in zip(headers, *rows)]
    _console.print(_row(headers, widths, ["bold"] * len(headers)))
    for row in rows:
        colour = SECRET_CHANGE_COLOURS[row[0]]
        _console.print(_row(list(row), widths, [colour, "bold"]))


def log_line(time: str, level: str, message: str) -> None:
    """One line of a job's log: its time, its level coloured, then the message as is."""
    line = Text(f"{time:<8} ")
    line.append(f"{level:<7}", style=LOG_LEVEL_COLOURS.get(level, ""))
    line.append(f" {message}")
    line.rstrip()
    _console.print(line)


def _answer(prompt: str, markup: bool = True, password: bool = False) -> str:
    """Read one answer; each line that another thread printed meanwhile prints after it. A
    Ctrl+C or the end of input ends the question line, so a line printed next, here or by
    the pdt process that started this provider script, starts a line of its own."""
    try:
        return _console.input(prompt, markup=markup, password=password)
    except (EOFError, KeyboardInterrupt):
        Console.print(_console)
        raise
    finally:
        drain()


def ask(question: str, default: str = "") -> str:
    suffix = f" [{default}]" if default != "" else ""
    return _answer(f"{question}{suffix}: ", markup=False).strip()


def read(prompt: str) -> str:
    """The line the user types after `prompt`, which prints as is."""
    return _answer(prompt, markup=False).strip()


def secret(prompt: str) -> str:
    """The line the user types after `prompt`, which does not show as it is typed."""
    return _answer(prompt, markup=False, password=True).strip()


YES_NO = {"y": True, "yes": True, "n": False, "no": False}


def confirm(question: str = "Proceed?", default: bool = False, choices: str = "") -> bool:
    """Ask until the answer is y, yes, n, or no in any case; Enter takes the default.
    `choices` replaces the [y/N] after the question.

    End of input raises EOFError."""
    choices = escape(choices) or ("\\[Y/n]" if default else "\\[y/N]")
    while True:
        answer = _answer(f"[bold]{escape(question)}[/] {choices} ").strip().lower()
        if answer == "":
            return default
        if answer in YES_NO:
            return YES_NO[answer]
        _console.print("Please answer y or n.")


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
    _console.print(f"[bold]Estimated monthly cost[/] ({escape(prices)}):")
    for label, amount in items:
        _console.print(f"  {escape(label):<{width}}  [cyan]{symbol}{amount:>7.2f}[/]")
    total = sum(amount for _, amount in items)
    _console.print(
        f"  [bold]{'total':<{width}}  [cyan]{escape(total_symbol)}{total:>7.2f}[/][/]")
    if excludes != "":
        _console.print(f"  [dim]{escape(excludes)}[/]")


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
    _console.print(_row(headers, widths, ["bold"] * len(headers)))
    for index, row in enumerate(cells):
        _console.print(_row(row, widths, row_styles[index] if row_styles else styles or []))
