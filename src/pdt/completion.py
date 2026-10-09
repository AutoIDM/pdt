from __future__ import annotations

import glob
import os
import shlex
import stat
import subprocess
import sys
from pathlib import Path

import argcomplete
import shellingham
from argcomplete.completers import ChoicesCompleter, DirectoriesCompleter, SuppressCompleter

from pdt import config, console, scaffold, storage_cli
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


def files(prefix: str, **_kwargs) -> list[str]:
    # argcomplete's FilesCompleter runs bash, which Windows does not have.
    return [path + "/" if os.path.isdir(path) else path
            for path in sorted(glob.glob(glob.escape(prefix) + "*"))]


def storage_args(prefix: str, parsed_args, **_kwargs) -> list[str]:
    """`pdt storage APP get KEY [PATH]` takes a local path; the other words are not paths."""
    rest = parsed_args.rest or []
    if not rest:
        return [name for name in storage_cli.COMMANDS if name.startswith(prefix)]
    return files(prefix) if rest[0] == "get" and len(rest) == 2 else []


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
    # use_defaults=False stops bash from offering file names when pdt offers nothing.
    code = argcomplete.shellcode(executables, use_defaults=False, shell=name)
    if name == "powershell":
        # PowerShell offers file names when a completer returns nothing, but not when it
        # returns one empty string.
        code = code.replace("    Remove-Item $completion_file",
                            '    if (-not (Get-Content $completion_file)) { "" }\n'
                            "    Remove-Item $completion_file")
    if name == "zsh":
        # The script registers itself with compdef, which only exists after compinit.
        code = ("if ! (( $+functions[compdef] )); then\n  autoload -Uz compinit\n  compinit\nfi\n"
                + code)
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
    # A positional with no completer offers nothing, not argcomplete's default of file names.
    argcomplete.autocomplete(parser, always_complete_options=False,
                            default_completer=SuppressCompleter())
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
        console.say("Tell it: " + ", ".join(console.value(f"pdt completion {name}")
                                            for name in SHELLS))
        return 1
    if print_only:
        # The user pipes this into a startup file, so it goes out byte for byte.
        sys.stdout.write(_script(shell))
        return 0
    setup(shell)
    script, startup = _paths(shell)
    console.done(f"Tab completion for pdt is set up in {console.value(startup or script)}")
    if shell in {"powershell", "pwsh"} and os.name == "nt":
        _allow_profile(shell)
    else:
        console.say("It works in every new terminal.")
    _this_window(shell)
    return 0


def _this_window(shell: str) -> None:
    # A command cannot change the shell session that started it, so the user runs a
    # line that loads the script into it. In PowerShell, Invoke-Expression runs text,
    # which no execution policy blocks.
    if shell in {"bash", "zsh"}:
        # macOS ships bash 3.2, whose `source <(...)` reads nothing.
        line = f'eval "$(pdt completion {shell} --script)"'
    elif shell == "fish":
        line = "pdt completion fish --script | source"
    elif shell in {"powershell", "pwsh"}:
        line = f"pdt completion {shell} --script | Out-String | Invoke-Expression"
    else:
        return
    console.say("To use it in this window now, run:")
    console.command(line)


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
    console.warn(f"PowerShell's execution policy ({console.value(policy)}) stops new windows from "
                 "loading tab completion.")
    if can_prompt(None) and console.confirm(
            "Allow PowerShell to run scripts that you create on this computer?"):
        _powershell(shell, f"{ALLOW_PROFILE} -Force")
        policy = _execution_policy(shell)
        if policy.lower() not in BLOCKING_POLICIES:
            console.done(f"Execution policy for your account is now {console.value(policy)}. "
                         "Tab completion works in every new PowerShell window.")
            return
        console.note(f"the execution policy is still {console.value(policy)}; a Group Policy on "
                     "this computer sets it, so ask whoever manages the computer to allow "
                     f"{console.value('RemoteSigned')}.")
        return
    console.say("To load it in every new PowerShell window, run this once:")
    console.command(ALLOW_PROFILE)
