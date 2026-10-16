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

On a terminal, a line wraps at a space to the terminal's width, and each
line after the first starts under the text after the line's label or
indent. A word too wide for a line, such as a long path or URL, gets a
line of its own with no indent, so a copy of that line holds it whole. A
table too wide for the terminal prints one block per row: the first cell,
then `name: cell` for each other cell. Output that is not a terminal never
wraps, so a log holds one line per message.
"""

from __future__ import annotations

import os
import re
import sys
from contextlib import contextmanager
from time import monotonic
from typing import Iterator

from rich.cells import cell_len
from rich.console import Console
from rich.markup import escape
from rich.text import Text

# soft_wrap keeps a long path or command on one logical line: the terminal
# wraps it at the edge, and rich never re-flows it at a word boundary.
_console = Console(highlight=False, soft_wrap=True, emoji=False)
# For a line from another thread that must reach stderr while _console prints on.
_errors = Console(stderr=True, highlight=False, soft_wrap=True, emoji=False)

# The real stdout, for `data`, once `json_output` has moved stdout to stderr.
_data = None

# Each message `warn_once` or `status_once` has printed in this process.
_warned: set[str] = set()

INDENT = 6


def width() -> int:
    return _console.width


def _wrap(line: Text, hang: int, room: int = 0) -> list[Text]:
    """`line` cut at spaces into lines of `room` cells (default: the terminal's width),
    each line after the first `hang` spaces in. A value() never breaks. A word too wide
    for a line gets a line of its own with no indent."""
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
        breaks, alone = [], set()
        used = hang
        content = words != [] and paragraph.plain[:words[0][0]].strip() != ""
        for start, end in words:
            size = cell_len(paragraph.plain[start:end].rstrip())
            if size > room - hang:
                breaks += [end] if start in (0, *breaks[-1:]) else [start, end]
                alone.add(start)
                used, content = hang, False
                continue
            if content and used + size > room:
                breaks.append(start)
                used = hang
            used += cell_len(paragraph.plain[start:end])
            content = True
        for offset, part in zip([0, *breaks], paragraph.divide(breaks)):
            if offset == 0 and breaks and not part.plain.strip():
                continue
            if offset == 0 or offset in alone:
                lines.append(part)
            elif part.plain.strip():
                lines.append(Text(" " * hang) + part)
    for part in lines:
        part.rstrip()
    return lines


def _print(message: str | Text, hang: int | None = None) -> None:
    """Print marked-up `message`, wrapped on a terminal. `hang` defaults to the spaces
    that start `message`."""
    if not _console.is_terminal:
        _console.print(message)
        return
    line = Text.from_markup(message, emoji=False) if isinstance(message, str) else message
    if hang is None:
        hang = len(line.plain) - len(line.plain.lstrip(" "))
    for part in _wrap(line, hang):
        _console.print(part)


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


def say(message: str = "") -> None:
    """A line with no colour."""
    _print(message)


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


def status_once(message: str) -> None:
    """A status line that several reads may each start, such as a price fetch: only its
    first time prints."""
    if message not in _warned:
        _warned.add(message)
        status(message)


class Wait:
    """What pdt waits for, and the seconds since it started waiting for it."""

    def __init__(self, message: str) -> None:
        self.now(message)

    def now(self, message: str) -> None:
        if getattr(self, "message", None) != message:
            self.message, self.start = message, monotonic()

    def __rich__(self) -> Text:
        """One line, cut short with an ellipsis, so a narrow terminal never wraps it."""
        line = Text.from_markup(f"[dim]{self.message} ({round(monotonic() - self.start)}s)[/]",
                                emoji=False, overflow="ellipsis")
        line.no_wrap = True
        return line


@contextmanager
def waiting(message: str) -> Iterator[Wait]:
    """On a terminal, a spinner line under the output that names the wait and counts its seconds."""
    wait = Wait(message)
    if not _console.is_terminal:
        yield wait
        return
    with _console.status(wait):
        yield wait


def field(label: str, value: str) -> None:
    """A label and the value pdt found for it: an account, a URL, a path."""
    _print(f"{escape(label)}: [bold]{escape(value)}[/]", len(label) + 2)


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


def event(text: str, mark: str = "", mark_style: str = "") -> None:
    """One lifecycle event (pdt.lifecycle), bold magenta so it stands out in a run's output.
    `mark` names how it reached the console, such as `unhandled` (no alerts entry names
    the event) or `not delivered to <type>`, in `mark_style` when one is given."""
    styled = f"[{mark_style}]{escape(mark)}[/]" if mark_style else escape(mark)
    prefix = f"{styled} " if mark else ""
    _print(f"[bold magenta]\\[event] {prefix}{escape(text)}[/]", len("[event] "))


def event_failed(message: str) -> None:
    """A lifecycle event that an alerts transport could not deliver, on stderr."""
    _errors.print(f"[yellow]{message}[/]")


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
    line = Text.from_markup(" " * indent + text, emoji=False)
    for part in _wrap(line, len(line.plain) - len(line.plain.lstrip(" "))):
        _console.print(part)


def bullet(text: str, indent: int = 2) -> None:
    """A marked-up line, indented, and each line it wraps to two spaces further in. Pass
    text that pdt did not write through escape()."""
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
    elif _console.is_terminal and indent + len(text) + 2 + len(note) > _console.width:
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
                      "running": "cyan", "not yet run": "dim"}
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


def _input(prompt: Text) -> str:
    """The answer to `prompt`. On a terminal the prompt wraps, and the answer starts after
    its last line."""
    if _console.is_terminal:
        *lines, prompt = _wrap(prompt, 0)
        for line in lines:
            _console.print(line)
        prompt.append(" ")
    return _console.input(prompt)


def ask(question: str, default: str = "") -> str:
    suffix = f" [{default}]" if default != "" else ""
    return _input(Text(f"{question}{suffix}: ")).strip()


def confirm(question: str = "Proceed?") -> bool:
    return _input(Text.from_markup(f"[bold]{escape(question)}[/] \\[y/N] ")).strip().lower() in (
        "y", "yes")


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
    if _console.is_terminal and sum(widths) + 2 * (len(widths) - 1) > _console.width:
        _stacked(headers, cells, row_styles or [styles or []] * len(cells))
        return
    _console.print(_row(headers, widths, ["bold"] * len(headers)))
    for index, row in enumerate(cells):
        _console.print(_row(row, widths, row_styles[index] if row_styles else styles or []))


def _stacked(headers: list[str], rows: list[list[str]], row_styles: list[list[str]]) -> None:
    """A table too wide for the terminal: each row's first cell bold on a line of its own,
    then each other cell that is not empty as `name: cell`, as many to a line as fit."""
    for row, styles in zip(rows, row_styles):
        styles = styles + [""] * (len(row) - len(styles))
        _print(Text(row[0], style=f"bold {styles[0]}".strip()))
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
