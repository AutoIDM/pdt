"""One place for terminal output.

Rich drops the colour when output is not a terminal, so a redirected or
piped run stays plain text and every command stays readable in a log.

The vocabulary is small on purpose. Bold marks a value the user may
type back or copy: a name, a command, a path, an id. Cyan marks a value
pdt worked out for the user to weigh: a numbered choice, a dollar
amount, the `==>` of a step. Dim marks text the user may skip: a
progress line, a side note. Colour otherwise marks state and nothing
else: red for a failure, green for a success, yellow for something the
user should read but need not act on. Everything else prints unstyled.

Import the module, not its functions: `from pdt import console`, then
`console.error(...)`. The names here are short and would collide.
"""

from __future__ import annotations

import textwrap

from rich.console import Console
from rich.markup import escape
from rich.text import Text

# soft_wrap keeps a long path or command on one logical line: the terminal
# wraps it at the edge, and rich never re-flows it at a word boundary.
_console = Console(highlight=False, soft_wrap=True)

INDENT = 6


def width() -> int:
    return _console.width


def say(message: str = "") -> None:
    """A plain line. Square brackets and :emoji: codes in `message` stay literal."""
    _console.print(message, markup=False, emoji=False)


def progress(text: str) -> None:
    """A counter that ticks in place. Call say() to end the line."""
    _console.print(text, markup=False, end="\r")


def styled(message: str) -> None:
    """A line the caller has already marked up."""
    _console.print(message)


def status(message: str) -> None:
    """A line about work in progress, such as a fetch or a login."""
    _console.print(f"[dim]{escape(message)}[/]")


def field(label: str, value: str) -> None:
    """A label and the value pdt found for it: an account, a URL, a path."""
    _console.print(f"{escape(label)}: [bold]{escape(value)}[/]")


def choice(number: int, label: str, detail: str = "") -> None:
    """One numbered option the user picks with `number`."""
    line = f"  [bold cyan]{number})[/] {escape(label)}"
    if detail != "":
        line += f"  [dim]{escape(detail)}[/]"
    _console.print(line)


def error(message: str) -> None:
    _console.print(f"[bold red]error:[/] {escape(message)}")


def note(message: str) -> None:
    """Something pdt did not do, and why."""
    _console.print(f"[yellow]note:[/] {escape(message)}")


def warn(message: str) -> None:
    """A caution with no label, such as a re-prompt after a bad answer."""
    _console.print(f"[yellow]{escape(message)}[/]")


def step(message: str) -> None:
    """One reconcile action, printed as it happens."""
    _console.print(f"[bold cyan]==>[/] {escape(message)}")


def done(message: str) -> None:
    _console.print(f"[bold green]{escape(message)}[/]")


def failed(message: str) -> None:
    _console.print(f"[bold red]{escape(message)}[/]")


def heading(message: str) -> None:
    _console.print(f"[bold]{escape(message)}[/]")


def name(text: str) -> None:
    """A thing the user can type back to pdt: an app, an example."""
    _console.print(f"  [bold cyan]{escape(text)}[/]")


def detail(text: str, indent: int = INDENT) -> None:
    """Prose under a name, wrapped to the terminal and indented."""
    _console.print(textwrap.fill(text, width=_console.width,
                                 initial_indent=" " * indent,
                                 subsequent_indent=" " * indent),
                   markup=False)


def bullet(text: str, indent: int = 2) -> None:
    _console.print(f"{' ' * indent}{text}", markup=False)


def command(text: str, note: str = "", indent: int = 2) -> None:
    """A command line or a path the user can copy, with an optional note."""
    if note != "":
        _console.print(f"{' ' * indent}[bold]{escape(text)}[/]  [dim]{escape(note)}[/]")
    else:
        _console.print(f"{' ' * indent}[bold]{escape(text)}[/]")


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
        _console.print(_row(list(row), widths, [SECRET_CHANGE_COLOURS[row[0]]] * len(row)))


def log_line(time: str, level: str, message: str) -> None:
    """One line of a job's log: its time, its level coloured, then the message as is."""
    line = Text(f"{time:<8} ")
    line.append(f"{level:<7}", style=LOG_LEVEL_COLOURS.get(level, ""))
    line.append(f" {message}")
    line.rstrip()
    _console.print(line)


def ask(question: str, default: str = "") -> str:
    suffix = f" [{default}]" if default != "" else ""
    return _console.input(f"{question}{suffix}: ", markup=False).strip()


def confirm(question: str = "Proceed?", default: bool = False) -> bool:
    options = "Y/n" if default else "y/N"
    answer = _console.input(f"[bold]{escape(question)}[/] \\[{options}] ").strip().lower()
    return default if answer == "" else answer in ("y", "yes")


def _row(cells: list[str], widths: list[int], styles: list[str]) -> Text:
    line = Text()
    for index, cell in enumerate(cells):
        style = styles[index] if index < len(styles) else ""
        line.append(cell, style=style or "")
        if index < len(cells) - 1:
            line.append(" " * (widths[index] - len(cell) + 2))
    line.rstrip()
    return line


def cost(items: list[tuple[str, float]], prices: str, excludes: str = "") -> None:
    """A monthly cost estimate: one line per item, then the total."""
    width = max(len(label) for label, _ in items)
    _console.print(f"[bold]Estimated monthly cost[/] ({escape(prices)}):")
    for label, amount in items:
        _console.print(f"  {escape(label):<{width}}  [cyan]${amount:>7.2f}[/]")
    total = sum(amount for _, amount in items)
    _console.print(f"  [bold]{'total':<{width}}  [cyan]${total:>7.2f}[/][/]")
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
