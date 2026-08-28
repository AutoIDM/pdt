"""One place for terminal output.

Rich drops the colour when output is not a terminal, so a redirected or
piped run stays plain text and every command stays readable in a log.

The vocabulary is small on purpose. Bold marks a name or a command.
Colour marks state and nothing else: red for a failure, green for a
success, yellow for something the user should read but need not act on.
Everything else prints unstyled.

Import the module, not its functions: `from pdt import console`, then
`console.error(...)`. The names here are short and would collide.
"""

from __future__ import annotations

import textwrap

from rich.console import Console
from rich.text import Text

# soft_wrap keeps a long path or command on one logical line: the terminal
# wraps it at the edge, and rich never re-flows it at a word boundary.
_console = Console(highlight=False, soft_wrap=True)

INDENT = 6


def width() -> int:
    return _console.width


def say(message: str = "") -> None:
    """A plain line. Square brackets in `message` stay literal."""
    _console.print(message, markup=False)


def styled(message: str) -> None:
    """A line the caller has already marked up."""
    _console.print(message)


def error(message: str) -> None:
    _console.print(f"[bold red]error:[/] {message}", markup=True)


def note(message: str) -> None:
    """Something pdt did not do, and why."""
    _console.print(f"[yellow]note:[/] {message}")


def warn(message: str) -> None:
    """A caution with no label, such as a re-prompt after a bad answer."""
    _console.print(f"[yellow]{message}[/]")


def step(message: str) -> None:
    """One reconcile action, printed as it happens."""
    _console.print(f"[bold cyan]==>[/] {message}")


def done(message: str) -> None:
    _console.print(f"[bold green]{message}[/]")


def heading(message: str) -> None:
    _console.print(f"[bold]{message}[/]")


def name(text: str) -> None:
    """A thing the user can type back to pdt: an app, an example."""
    _console.print(f"  [bold cyan]{text}[/]")


def detail(text: str, indent: int = INDENT) -> None:
    """Prose under a name, wrapped to the terminal and indented."""
    _console.print(textwrap.fill(text, width=_console.width,
                                 initial_indent=" " * indent,
                                 subsequent_indent=" " * indent),
                   markup=False)


def bullet(text: str, indent: int = 2) -> None:
    _console.print(f"{' ' * indent}{text}", markup=False)


def command(text: str, indent: int = 2) -> None:
    """A command line the user can copy."""
    _console.print(f"{' ' * indent}[bold]{text}[/]")


def ask(question: str, default: str = "") -> str:
    suffix = f" [{default}]" if default != "" else ""
    return _console.input(f"{question}{suffix}: ", markup=False).strip()


def confirm(question: str = "Proceed?") -> bool:
    return _console.input(f"[bold]{question}[/] \\[y/N] ").strip().lower() in ("y", "yes")


def _row(cells: list[str], widths: list[int], styles: list[str]) -> Text:
    line = Text()
    for index, cell in enumerate(cells):
        style = styles[index] if index < len(styles) else ""
        line.append(cell, style=style or "")
        if index < len(cells) - 1:
            line.append(" " * (widths[index] - len(cell) + 2))
    return line


def table(headers: list[str], rows: list[list[str]],
          styles: list[str] | None = None) -> None:
    """Aligned columns. Cell text is literal, and no line ends in a space."""
    cells = [[str(cell) for cell in row] for row in rows]
    widths = [max(len(row[index]) for row in (headers, *cells))
              for index in range(len(headers))]
    _console.print(_row(headers, widths, ["bold"] * len(headers)))
    for row in cells:
        _console.print(_row(row, widths, styles or []))
