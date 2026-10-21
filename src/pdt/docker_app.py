"""A Docker app: an app folder with a Dockerfile and neither run.py nor a .ps1 file.

The image holds the program, in any language. pdt reads the command the
image starts (the ENTRYPOINT and CMD of the Dockerfile's last stage) from
the Dockerfile itself, so the user writes an ordinary Dockerfile and adds no
config key. A deploy appends deploy_common.DOCKER_APP_TAIL, which starts
that command through `pdt.run_docker` inside the job lifecycle. `pdt run`
builds the Dockerfile as it is and runs the same command in a container of
it, with `run_docker.py --image`.

`config.docker_app` decides what a Docker app is; this module reads its
Dockerfile.
"""

from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass
from pathlib import Path

from pdt import config, console

DEFAULT_SHELL = ["/bin/sh", "-c"]
HEREDOC = re.compile(r"<<-?\s*[\"']?(\w+)[\"']?")
STATE_ENV = "PDT_STATE_DIR"
SCAN_LIMIT_BYTES = 1_000_000


@dataclass
class Stage:
    """What the last stage of a Dockerfile starts, and as which user.

    `command` is [] when the stage names no ENTRYPOINT and no CMD. `source`
    names the instructions it came from. `user` is the stage's last USER, or
    "" when it sets none.
    """
    command: list[str]
    source: str
    user: str


def instructions(text: str) -> list[tuple[str, str]]:
    """(keyword, arguments) of each instruction, with comments, continuations, and
    heredoc bodies taken out."""
    escape = "\\"
    lines = text.splitlines()
    for line in lines:
        directive = re.fullmatch(r"#\s*escape\s*=\s*(\S)\s*", line.strip(), re.IGNORECASE)
        if directive:
            escape = directive.group(1)
        elif not re.fullmatch(r"#\s*\w+\s*=.*", line.strip()):
            break
    found = []
    current = ""
    heredoc = ""
    for line in lines:
        if heredoc:
            if line.strip() == heredoc:
                heredoc = ""
            continue
        stripped = line.strip()
        if stripped.startswith("#") or (stripped == "" and current == ""):
            continue
        if stripped.endswith(escape):
            current += stripped[:-1] + " "
            continue
        current += stripped
        keyword, _, arguments = current.strip().partition(" ")
        current = ""
        if keyword:
            found.append((keyword.upper(), arguments.strip()))
        match = HEREDOC.search(arguments)
        if match and keyword.upper() in ("RUN", "COPY", "ADD"):
            heredoc = match.group(1)
    return found


def _exec_form(arguments: str) -> list[str] | None:
    if not arguments.startswith("["):
        return None
    try:
        value = json.loads(arguments)
    except json.JSONDecodeError:
        return None
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return value
    return None


def final_stage(text: str) -> Stage:
    entrypoint = cmd = None
    user = ""
    shell = DEFAULT_SHELL
    for keyword, arguments in instructions(text):
        if keyword == "FROM":
            entrypoint = cmd = None
            user, shell = "", DEFAULT_SHELL
        elif keyword == "SHELL":
            shell = _exec_form(arguments) or shell
        elif keyword == "USER":
            user = arguments
        elif keyword in ("ENTRYPOINT", "CMD"):
            form = _exec_form(arguments)
            value = ("exec", form) if form is not None else ("shell", [*shell, arguments])
            if keyword == "ENTRYPOINT":
                entrypoint = value
            else:
                cmd = value
    if entrypoint is not None and entrypoint[0] == "shell":
        return Stage(entrypoint[1], "ENTRYPOINT", user)
    if entrypoint is not None:
        command = entrypoint[1] + (cmd[1] if cmd is not None else [])
        return Stage(command, "ENTRYPOINT and CMD" if cmd is not None else "ENTRYPOINT", user)
    if cmd is not None:
        return Stage(cmd[1], "CMD", user)
    return Stage([], "", user)


def stage_of(app: dict) -> Stage:
    return final_stage((Path(app["dir"]) / "Dockerfile").read_text(encoding="utf-8"))


def command_problem(app: dict) -> str:
    """Why pdt cannot tell what the app's image runs, or ""."""
    if stage_of(app).command:
        return ""
    return (f"{app['name']}/Dockerfile names no command for the job to run; add a CMD line at "
            'the end of it, for example CMD ["node", "index.js"]')


def state_reader(app_dir: Path) -> str:
    """The first file of the app folder that names PDT_STATE_DIR, or "". A Docker app's code
    can be in any language, so pdt looks for the name, not for a read of it."""
    for path in sorted(app_dir.rglob("*")):
        relative = path.relative_to(app_dir)
        if (any(part.startswith(".") for part in relative.parts) or not path.is_file()
                or path.name == config.APP_FILE or path.stat().st_size > SCAN_LIMIT_BYTES):
            continue
        if STATE_ENV.encode() in path.read_bytes():
            return relative.as_posix()
    return ""


def summary_lines(app: dict) -> list[str]:
    """What a Docker app runs and where pdt found it, as lines for the user."""
    stage = stage_of(app)
    dockerfile = f"{app['name']}/Dockerfile"
    lines = [f"runs {console.value(shlex.join(stage.command))}, the {stage.source} of "
             f"{console.value(dockerfile)}"]
    reader = state_reader(Path(app["dir"]))
    if reader:
        lines.append(f"keeps {console.value(STATE_ENV)} between runs "
                     f"[dim](named in {console.value(reader)})[/]")
    return lines
