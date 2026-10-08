from __future__ import annotations

import os
import shlex
import stat
import subprocess
import sys
from pathlib import Path

import argcomplete
import shellingham
from argcomplete.completers import ChoicesCompleter, DirectoriesCompleter

from pdt import config, console, scaffold
from pdt.deploy_common import SECRET_ACTIONS
from pdt.utils.email_auth import can_prompt

SHELLS = ("bash", "zsh", "fish", "powershell")

START = "# pdt completion start"
END = "# pdt completion end"

# Windows blocks every profile script under these execution policies.
# Restricted is the default on a Windows client (GitHub issue 133).
BLOCKING_POLICIES = {"restricted", "allsigned"}
ALLOW_PROFILE = "Set-ExecutionPolicy -Scope CurrentUser RemoteSigned"


def apps(prefix: str, **_kwargs) -> list[str]:
    try:
        return [name for name in config.find_apps() if name.startswith(prefix)]
    except (config.ConfigError, OSError):
        return []


def examples(prefix: str, **_kwargs) -> list[str]:
    return [example.name for example in scaffold.examples()
            if example.name.startswith(prefix)]


directories = DirectoriesCompleter()
secret_actions = ChoicesCompleter(SECRET_ACTIONS)


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
    # On Windows, "powershell" covers both Windows PowerShell and PowerShell 7.
    both = shell == "cmd" or (shell == "powershell" and os.name == "nt")
    shells = ["powershell", "pwsh"] if both else [shell]
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
    if shell in {"powershell", "pwsh"} and os.name == "nt":
        _allow_profile(shell)
        _this_window(shell)
        return 0
    console.say("It works in every new terminal.")
    return 0


def _this_window(shell: str) -> None:
    # A command cannot change the PowerShell session that started it, and
    # Invoke-Expression runs text, which no execution policy blocks.
    console.say("To use it in this window now, run:")
    console.command(f"pdt completion {shell} --script | Out-String | Invoke-Expression")


def _powershell(shell: str, command: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["pwsh" if shell == "pwsh" else "powershell", "-NoLogo", "-NoProfile",
         "-NonInteractive", "-Command", command],
        stdin=subprocess.DEVNULL, capture_output=True, text=True)


def _execution_policy(shell: str) -> str:
    try:
        return _powershell(shell, "Get-ExecutionPolicy").stdout.strip()
    except OSError:
        return ""


def _allow_profile(shell: str) -> None:
    """Make sure new windows load the profile, which the execution policy may block."""
    policy = _execution_policy(shell)
    if policy.lower() not in BLOCKING_POLICIES:
        console.say("It works in every new PowerShell window.")
        return
    console.warn(f"PowerShell's execution policy ({policy}) stops new windows from "
                 "loading tab completion.")
    if can_prompt(None) and console.confirm(
            "Allow PowerShell to run scripts that you create on this computer?"):
        _powershell(shell, f"{ALLOW_PROFILE} -Force")
        policy = _execution_policy(shell)
        if policy.lower() not in BLOCKING_POLICIES:
            console.done(f"Execution policy for your account is now {policy}. "
                         "Tab completion works in every new PowerShell window.")
            return
        console.note(f"the execution policy is still {policy}; a Group Policy on this "
                     "computer sets it, so ask whoever manages the computer to allow "
                     "RemoteSigned.")
        return
    console.say("To load it in every new PowerShell window, run this once:")
    console.command(ALLOW_PROFILE)
