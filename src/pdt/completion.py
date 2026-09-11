"""Shell tab completion for the pdt command.

`pdt completion` writes a small script into the user's shell startup file. That
script asks `pdt _complete` for candidates each time the user presses Tab, so
the shell never has to know the project's app names or the command list. Both
come from the live argparse parser and the project on disk.

The shell passes the words typed so far and the index of the word under the
cursor. `candidates` walks the parser: the first word completes to a command,
options complete to that command's flags, and a positional completes to
whatever its action's `completer` names (app names, example names) or, failing
that, its argparse `choices`.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

from pdt import console
from pdt.config import data_home
from pdt.utils.email_auth import can_prompt

# Values for an argparse action's `completer` attribute. cli.py marks the
# arguments that take an app name or an example name with these.
APPS = "apps"
EXAMPLES = "examples"

SHELLS = ("bash", "zsh", "fish", "powershell")

BEGIN = "# >>> pdt completion >>>"
END = "# <<< pdt completion <<<"

# Each script calls the command as the user typed it (`pdt` or `./pdt`), so a
# clone without an installed `pdt` completes too. The 0-based index of the word
# under the cursor comes first, then every word on the line.
SCRIPTS = {
    "bash": """\
_pdt_complete() {
    local IFS=$'\\n'
    COMPREPLY=( $("${COMP_WORDS[0]}" _complete "$COMP_CWORD" "${COMP_WORDS[@]}" 2>/dev/null) )
}
complete -F _pdt_complete pdt ./pdt
""",
    "zsh": """\
if ! typeset -f compdef >/dev/null; then
    autoload -Uz compinit && compinit
fi
_pdt_complete() {
    local -a found
    found=(${(f)"$("$words[1]" _complete $((CURRENT - 1)) "${words[@]}" 2>/dev/null)"})
    compadd -a found
}
compdef _pdt_complete pdt
""",
    "fish": """\
function __pdt_complete
    set -l tokens (commandline -opc)
    set -l current (commandline -ct)
    $tokens[1] _complete (count $tokens) $tokens $current 2>/dev/null
end
complete -c pdt -f -a '(__pdt_complete)'
""",
    "powershell": """\
$PdtCompleter = {
    param($wordToComplete, $commandAst, $cursorPosition)
    $words = @($commandAst.CommandElements | ForEach-Object { $_.Extent.Text })
    $cword = if ($wordToComplete) { $words.Count - 1 } else { $words.Count }
    & $words[0] _complete $cword @words 2>$null | ForEach-Object {
        [System.Management.Automation.CompletionResult]::new($_, $_, 'ParameterValue', $_)
    }
}
Register-ArgumentCompleter -Native -CommandName pdt, pdt.exe, pdt.bat -ScriptBlock $PdtCompleter
""",
}


def complete(parser: argparse.ArgumentParser, argv: list[str]) -> int:
    """Entry point for the hidden `pdt _complete CWORD WORDS...` command.

    The candidates go straight to stdout, not through `console`: the shell reads
    them, one per line, and any styling or re-flow would corrupt them.
    """
    try:
        cword = int(argv[0])
        words = argv[1:]
        for name in candidates(parser, cword, words):
            sys.stdout.write(name + "\n")
    except Exception:  # noqa: BLE001 - a completion must never show an error
        pass
    return 0


def candidates(parser: argparse.ArgumentParser, cword: int, words: list[str]) -> list[str]:
    """Return the completions for the word at `words[cword]`.

    `words[0]` is the command itself. A missing `words[cword]` means the user
    pressed Tab after a space, so the word under the cursor is empty.
    """
    current = words[cword] if cword < len(words) else ""
    before = words[1:cword]
    if not before:
        if current.startswith("-"):
            return _matching(_option_strings(parser), current)
        return _matching(_command_names(parser), current)
    sub = _subparsers(parser).get(before[0])
    if sub is None:
        return []
    if current.startswith("-"):
        return _matching(_option_strings(sub), current)
    options = {s: a for a in sub._actions for s in a.option_strings}
    positionals = [a for a in sub._actions if not a.option_strings]
    filled = 0
    pending = None  # the option whose value the current word supplies
    for token in before[1:]:
        if pending is not None:
            pending = None
            continue
        if token.startswith("-"):
            action = options.get(token.split("=", 1)[0])
            if action is not None and action.nargs != 0 and "=" not in token:
                pending = action
            continue
        filled += 1
    if pending is not None:
        return _matching(_values_for(pending), current)
    if filled < len(positionals):
        return _matching(_values_for(positionals[filled]), current)
    return []


def _values_for(action: argparse.Action) -> list[str]:
    completer = getattr(action, "completer", None)
    if completer == APPS:
        return _app_names()
    if completer == EXAMPLES:
        return _example_names()
    if action.choices:
        return [str(choice) for choice in action.choices]
    return []


def _app_names() -> list[str]:
    from pdt import config

    try:
        return config.find_apps()
    except Exception:  # noqa: BLE001 - outside a project there are no apps
        return []


def _example_names() -> list[str]:
    from pdt import scaffold

    return [example.name for example in scaffold.examples()]


def _subparsers(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return dict(action.choices)
    return {}


def _command_names(parser: argparse.ArgumentParser) -> list[str]:
    return [name for name in _subparsers(parser) if not name.startswith("_")]


def _option_strings(parser: argparse.ArgumentParser) -> list[str]:
    return [s for a in parser._actions for s in a.option_strings if s.startswith("--")]


def _matching(names: list[str], prefix: str) -> list[str]:
    return [name for name in names if name.startswith(prefix)]


def script(shell: str) -> str:
    return SCRIPTS[shell]


def detect_shell() -> str:
    """Name the shell the user is typing in, or "" when pdt cannot tell."""
    if os.name == "nt" and not os.environ.get("SHELL"):
        return "powershell"
    name = Path(os.environ.get("SHELL", "")).name
    if name in ("pwsh", "powershell"):
        return "powershell"
    return name if name in SHELLS else ""


def startup_file(shell: str) -> Path | None:
    """The file the shell reads when it starts, or None if pdt cannot find it."""
    home = Path.home()
    if shell == "bash":
        return home / ".bashrc"
    if shell == "zsh":
        return Path(os.environ.get("ZDOTDIR") or home) / ".zshrc"
    if shell == "fish":
        return home / ".config" / "fish" / "completions" / "pdt.fish"
    if shell == "powershell":
        exe = shutil.which("pwsh") or shutil.which("powershell")
        if not exe:
            return None
        proc = subprocess.run([exe, "-NoProfile", "-Command", "$PROFILE"],
                              capture_output=True, text=True)
        path = proc.stdout.strip()
        return Path(path) if proc.returncode == 0 and path else None
    return None


def write_block(path: Path, shell: str) -> None:
    """Put the completion script into `path`, replacing any earlier copy."""
    path.parent.mkdir(parents=True, exist_ok=True)
    body = script(shell).rstrip("\n")
    if shell == "fish":
        # Fish autoloads this file by name; it holds nothing else.
        path.write_text(body + "\n")
        return
    block = f"{BEGIN}\n{body}\n{END}\n"
    text = path.read_text() if path.exists() else ""
    if BEGIN in text and END in text:
        head, rest = text.split(BEGIN, 1)
        _, tail = rest.split(END, 1)
        text = head + block.rstrip("\n") + tail
    else:
        if text and not text.endswith("\n"):
            text += "\n"
        text += ("\n" if text else "") + block
    path.write_text(text)


def is_installed(path: Path, shell: str) -> bool:
    if not path.is_file():
        return False
    if shell == "fish":
        return True
    return BEGIN in path.read_text()


def answer_file() -> Path:
    """Records that pdt has offered tab completion once, and what the user said."""
    return data_home() / "pdt" / "completion-offered"


def can_ask() -> bool:
    return can_prompt(None)


def offer(assume_yes: bool = False) -> None:
    """Ask once, after a command has done its work, whether to turn on completion.

    The answer is remembered in the user's data folder so no later command asks
    again. Nothing is asked on a build server, without a terminal, with `--yes`,
    or when pdt cannot tell which shell the user has.
    """
    if assume_yes or not can_ask():
        return
    marker = answer_file()
    if marker.exists():
        return
    shell = detect_shell()
    if not shell:
        return
    path = startup_file(shell)
    if path is None:
        return
    if is_installed(path, shell):
        _remember(marker, "yes")
        return
    console.say()
    try:
        answer = input(f"Turn on tab completion for pdt in {shell}? [Y/n] ").strip().lower()
    except EOFError:
        return
    if answer in ("", "y", "yes"):
        _remember(marker, "yes")
        write_block(path, shell)
        console.done(f"Tab completion is set up in {path}. New terminals have it;")
        console.say(f"for this one, run: {reload_hint(shell, path)}")
    else:
        _remember(marker, "no")
        console.say("Fine. `pdt completion` turns it on whenever you like.")


def _remember(marker: Path, answer: str) -> None:
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(answer + "\n")


def reload_hint(shell: str, path: Path) -> str:
    if shell == "fish":
        return f"source {path}"
    if shell == "powershell":
        return ". $PROFILE"
    return f"source {path}"


def install(shell: str | None, print_only: bool) -> int:
    shell = shell or detect_shell()
    if not shell:
        console.error("pdt could not tell which shell you use.")
        console.say("Tell it: " + ", ".join(f"pdt completion {name}" for name in SHELLS))
        return 1
    if print_only:
        # The user pipes this into a startup file, so it goes out byte for byte.
        sys.stdout.write(script(shell))
        return 0
    path = startup_file(shell)
    if path is None:
        console.error(f"pdt could not find where {shell} keeps its startup file.")
        console.say("Add these lines to it yourself:")
        console.say()
        console.say(script(shell))
        return 1
    write_block(path, shell)
    _remember(answer_file(), "yes")
    console.done(f"Tab completion for pdt is set up in {path}")
    console.say("It works in every new terminal. To use it in this one, run:")
    console.command(reload_hint(shell, path))
    return 0
