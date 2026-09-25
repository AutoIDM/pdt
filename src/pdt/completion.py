from __future__ import annotations

import os
import shlex
import stat
import sys
from pathlib import Path

import argcomplete
import shellingham
from argcomplete.completers import DirectoriesCompleter

from pdt import config, console, scaffold

SHELLS = ("bash", "zsh", "fish", "powershell")

START = "# pdt completion start"
END = "# pdt completion end"


def apps(prefix: str, **_kwargs) -> list[str]:
    try:
        return [name for name in config.find_apps() if name.startswith(prefix)]
    except (config.ConfigError, OSError):
        return []


def examples(prefix: str, **_kwargs) -> list[str]:
    return [example.name for example in scaffold.examples()
            if example.name.startswith(prefix)]


directories = DirectoriesCompleter()


def _shell() -> str | None:
    try:
        name, _ = shellingham.detect_shell()
    except (shellingham.ShellDetectionFailure, OSError, RuntimeError):
        return None
    name = name.lower()
    if name in {"bash", "zsh", "fish", "cmd", "powershell", "pwsh"}:
        return name
    return None


def _data_dir() -> Path:
    return config.data_home() / "pdt"


def _script(shell: str) -> str:
    name = "powershell" if shell in {"powershell", "pwsh"} else shell
    if name == "powershell":
        executables = ["pdt", "pdt.bat", r".\pdt.bat"]
    else:
        executables = ["pdt", "./pdt"]
    code = argcomplete.shellcode(executables, shell=name)
    return f"{START}\n{code.rstrip()}\n{END}\n"


def _paths(shell: str) -> tuple[Path, Path | None]:
    data = _data_dir()
    if shell == "fish":
        config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
        return config_home / "fish" / "completions" / "pdt.fish", None
    if shell in {"powershell", "pwsh"}:
        if os.name == "nt":
            folder = "PowerShell" if shell == "pwsh" else "WindowsPowerShell"
            startup = Path.home() / "Documents" / folder / "profile.ps1"
        else:
            startup = Path.home() / ".config" / "powershell" / "profile.ps1"
        return data / "pdt.ps1", startup
    if shell == "zsh":
        startup = Path(os.environ.get("ZDOTDIR", Path.home())) / ".zshrc"
    else:
        startup = Path.home() / ".bashrc"
    return data / f"pdt.{shell}", startup


def _install(path: Path, content: str) -> None:
    if path.is_symlink():
        path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = stat.S_IMODE(path.stat().st_mode) if path.is_file() else None
    old = path.read_text() if path.is_file() else ""
    if START in old and END in old:
        before, rest = old.split(START, 1)
        _, after = rest.split(END, 1)
        updated = before + content.rstrip("\n") + after
    else:
        separator = "\n" if old and not old.endswith("\n") else ""
        updated = old + separator + content
    if updated == old:
        return
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(updated)
    if mode is not None:
        temporary.chmod(mode)
    temporary.replace(path)


def setup(shell: str | None = None) -> None:
    shell = shell or _shell()
    if shell is None:
        return
    shells = ["powershell", "pwsh"] if shell == "cmd" else [shell]
    for name in shells:
        script, startup = _paths(name)
        _install(script, _script(name))
        if startup is None:
            continue
        if name in {"powershell", "pwsh"}:
            path = str(script).replace("'", "''")
            source = f"{START}\n. '{path}'\n{END}\n"
        elif name == "zsh":
            source = (f"{START}\nif ! (( $+functions[compdef] )); then\n"
                      f"  autoload -Uz compinit\n  compinit\nfi\n"
                      f"source {shlex.quote(str(script))}\n{END}\n")
        else:
            source = f"{START}\n. {shlex.quote(str(script))}\n{END}\n"
        _install(startup, source)
        if name == "bash":
            login = next((path for path in [Path.home() / ".bash_profile",
                                            Path.home() / ".bash_login",
                                            Path.home() / ".profile"]
                          if path.is_file()), Path.home() / ".bash_profile")
            login_source = source
            if login.name == ".profile":
                login_source = (f"{START}\nif [ -n \"${{BASH_VERSION-}}\" ]; then\n"
                                f"  . {shlex.quote(str(script))}\nfi\n{END}\n")
            _install(login, login_source)


def configure(parser) -> None:
    argcomplete.autocomplete(parser, always_complete_options=False)
    if not (sys.stdin.isatty() and sys.stderr.isatty()):
        return
    try:
        setup()
    except Exception:
        pass


def install(shell: str | None, print_only: bool) -> int:
    """`pdt completion [SHELL] [--script]`: set up one named shell, or print its script."""
    shell = shell or _shell()
    if shell is None:
        console.error("pdt could not tell which shell you use.")
        console.say("Tell it: " + ", ".join(f"pdt completion {name}" for name in SHELLS))
        return 1
    if print_only:
        # The user pipes this into a startup file, so it goes out byte for byte.
        sys.stdout.write(_script(shell))
        return 0
    setup(shell)
    script, startup = _paths(shell)
    console.done(f"Tab completion for pdt is set up in {startup or script}")
    console.say("It works in every new terminal.")
    return 0
