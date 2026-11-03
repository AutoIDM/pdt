"""The closed list of operations the portal can ask for, and the checks on a command's token.

OPERATIONS maps each operation to its minimum level, its args, the pdt command line it runs,
its expiry, and its run limit. The portal holds the same table. A command line comes only from
`Operation.argv`, which reads args that `args_problem` already passed, so no free text, shell
string, or command line in a token can reach a program. `verify` is the one gate: it raises
Refused with a plain message for a command the machine must not run.
"""

from __future__ import annotations

import fnmatch
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import jwt

from pdt import config

ISSUER = "pdt-portal"
LEVELS = ("view", "run", "manage")
DEFAULT_LEVEL = "run"
COMMAND_ID = re.compile(r"cmd_\d{8}T\d{6}Z_[0-9a-f]{8}")
# The contract's NAME_RE, and no leading dash: `--all` or `--plan` as an app name would reach
# pdt as a flag.
NAME = re.compile(r"(?!\.{1,2}$)(?!-)[A-Za-z0-9._-]{1,128}")
RUN_NUMBER = re.compile(r"[0-9]{1,6}")
SNAPSHOT = re.compile(r"[0-9a-f]{64}")
ARG_RULES = {"project": NAME, "app": NAME, "preset": NAME, "run": RUN_NUMBER,
             "snapshot_id": SNAPSHOT}
PLACE = re.compile(r"(aws|azure|google-cloud):[A-Za-z0-9-]{1,64}:[a-z0-9-]{1,32}")
ROLE = re.compile(r"[A-Za-z0-9:/@._-]{1,256}")
HOUR = 3600
MINUTE = 60
JWT_LEEWAY_SECONDS = 60
# A worker runs customer files beside AutoIDM's own credentials, so it runs no customer code
# (`run` without `deployed`, run_snapshot) and takes no file sync. It opens no job's logs.
WORKER_OPERATIONS = ("list", "validate", "runs", "health", "plan", "run", "pause", "unpause",
                     "deploy", "destroy")
REFUSED_PRESET_NAMES = ("PATH", "PYTHON*", "LD_*", "DYLD_*", "UV_*", "PDT_INSTALLATION*")
ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class Refused(Exception):
    """A command the machine must not run. The message goes to the portal."""


def run_argv(args: dict) -> list[str]:
    return ["run", args["app"], *(["--deployed", "--follow"] if args["deployed"] else [])]


@dataclass(frozen=True)
class Operation:
    level: str
    required: tuple[str, ...]
    optional: tuple[str, ...]
    argv: Callable[[dict], list[str]]
    expires_seconds: int
    limit_seconds: int
    # A snapshot supplies the project: a new temporary folder, not the project on the machine.
    from_snapshot: bool = False


OPERATIONS = {
    "list": Operation("view", ("project",), (), lambda a: ["list", "--json"], HOUR, 5 * MINUTE),
    "validate": Operation("view", ("project",), ("app",), lambda a: ["validate"], HOUR, 5 * MINUTE),
    "runs": Operation("view", ("project", "app"), (), lambda a: ["runs", a["app"], "--json"],
                      HOUR, 5 * MINUTE),
    "logs": Operation("view", ("project", "app"), ("run",),
                      lambda a: ["logs", a["app"], *([a["run"]] if "run" in a else [])],
                      HOUR, 5 * MINUTE),
    "health": Operation("view", ("project",), (), lambda a: ["health", "--json"], HOUR, 5 * MINUTE),
    "plan": Operation("view", ("project", "app"), (), lambda a: ["deploy", a["app"], "--plan"],
                      HOUR, 15 * MINUTE),
    "run": Operation("run", ("project", "app", "deployed"), ("preset",), run_argv, HOUR, 6 * HOUR),
    "pause": Operation("run", ("project", "app"), (), lambda a: ["pause", a["app"]],
                       24 * HOUR, 15 * MINUTE),
    "unpause": Operation("run", ("project", "app"), (), lambda a: ["unpause", a["app"]],
                         24 * HOUR, 15 * MINUTE),
    # The child has no terminal, so `--yes` stands in for the question `pdt sync up` asks.
    "sync_up": Operation("manage", ("project",), (), lambda a: ["sync", "up", "--yes"],
                         24 * HOUR, 15 * MINUTE),
    "sync_down": Operation("manage", ("project", "snapshot_id"), (),
                           lambda a: ["sync", "down", "--snapshot", a["snapshot_id"]],
                           24 * HOUR, 15 * MINUTE),
    "run_snapshot": Operation("manage", ("project", "snapshot_id", "app"), ("preset",),
                              lambda a: ["run", a["app"]], HOUR, 6 * HOUR, from_snapshot=True),
    "deploy": Operation("manage", ("project", "app"), (), lambda a: ["deploy", a["app"], "--yes"],
                        24 * HOUR, HOUR),
    "destroy": Operation("manage", ("project", "app"), (), lambda a: ["destroy", a["app"], "--yes"],
                         15 * MINUTE, HOUR),
}


def args_problem(operation: Operation, args: object, worker: bool = False) -> str:
    """Why `args` do not fit `operation`, or "". A worker's project comes from a snapshot, so
    only a worker takes `snapshot_id` on an operation that does not list it."""
    if not isinstance(args, dict):
        return "the args are not an object"
    allowed = {*operation.required, *operation.optional, *(["snapshot_id"] if worker else [])}
    needed = [*operation.required, *(["snapshot_id"] if worker else [])]
    for name in args:
        if name not in allowed:
            return f"the arg {name!r} is not one this operation takes"
    for name in needed:
        if name not in args:
            return f"the arg {name} is missing"
    for name, value in args.items():
        if name == "deployed":
            if not isinstance(value, bool):
                return "the arg deployed is not true or false"
        elif not (isinstance(value, str) and ARG_RULES[name].fullmatch(value)):
            return f"the arg {name} does not have the form this operation takes"
    return ""


def command_line(operation: str, args: dict) -> list[str]:
    """The program and the arguments of a command, for a list form with no shell."""
    return [sys.executable, "-m", "pdt.cli", *OPERATIONS[operation].argv(args)]


def level_at_most(level: str, maximum: str) -> bool:
    return LEVELS.index(level) <= LEVELS.index(maximum)


def verify(token: str, command_id: str, installation_id: str, customer_id: str, keys: list[dict],
           maximum_level: str, worker: bool = False) -> dict:
    """The claims of a command's token, when every check passes. Raises Refused otherwise."""
    if not COMMAND_ID.fullmatch(str(command_id)):
        raise Refused("The command ID does not have the form pdt expects.")
    try:
        kid = jwt.get_unverified_header(token).get("kid")
    except (jwt.InvalidTokenError, TypeError, ValueError):
        raise Refused("The command token does not parse.") from None
    pem = next((key.get("pem") for key in keys
                if isinstance(key, dict) and key.get("kid") == kid), None)
    if pem is None:
        raise Refused("A key this computer does not know signed the command.")
    try:
        claims = jwt.decode(token, pem, algorithms=["ES256"], audience=installation_id,
                            issuer=ISSUER, leeway=JWT_LEEWAY_SECONDS,
                            options={"require": ["aud", "iss", "sub", "exp", "iat"]})
    except jwt.ExpiredSignatureError:
        raise Refused("The command expired.") from None
    except jwt.InvalidAudienceError:
        raise Refused("The command is for another installation.") from None
    except jwt.InvalidTokenError as e:
        raise Refused(f"The command token failed its check: {e}.") from None
    except ValueError:
        raise Refused("The pinned command key does not parse.") from None
    if claims["sub"] != command_id:
        raise Refused("The command token names another command.")
    if claims.get("customer_id") != customer_id:
        raise Refused("The command is for another customer.")
    name = claims.get("operation")
    if not isinstance(name, str) or name not in OPERATIONS:
        raise Refused(f"pdt does not know the operation {name!r}.")
    operation = OPERATIONS[name]
    problem = args_problem(operation, claims.get("args"), worker)
    if problem:
        raise Refused(f"The {name} command was refused: {problem}.")
    limit = claims.get("limit_seconds")
    if not (type(limit) is int and 0 < limit):
        raise Refused("The command has no run limit.")
    claims["limit_seconds"] = min(limit, operation.limit_seconds)
    if not level_at_most(operation.level, maximum_level):
        raise Refused(f"This computer allows at most the level {maximum_level}, and {name} "
                      f"needs {operation.level}.")
    grant = claims.get("grant")
    if worker:
        if name not in WORKER_OPERATIONS:
            raise Refused(f"A cloud worker does not run {name}.")
        if name == "run" and not claims["args"]["deployed"]:
            raise Refused("A cloud worker runs only the deployed job.")
        if not (isinstance(grant, dict)
                and isinstance(grant.get("place"), str) and PLACE.fullmatch(grant["place"])
                and isinstance(grant.get("role"), str) and ROLE.fullmatch(grant["role"])):
            raise Refused("The command has no valid grant for a cloud worker.")
    elif grant is not None:
        raise Refused("This computer is not a cloud worker, and the command names a grant.")
    return claims


def preset_problem(name: str, preset: object) -> str:
    """Why the machine refuses a preset, or "". Windows ignores the case of an env var name."""
    if not (isinstance(preset, dict)
            and isinstance(preset.get("env", {}), dict)
            and isinstance(preset.get("artifacts", False), bool)
            and set(preset) <= {"env", "artifacts"}):
        return (f"The preset {name} in {config.PROJECT_FILE} must hold only env (names and "
                "values) and artifacts (true or false).")
    for key, value in preset.get("env", {}).items():
        if not (isinstance(key, str) and ENV_NAME.fullmatch(key)):
            return f"The preset {name} sets {key!r}, which is not an env var name."
        if isinstance(value, (dict, list)) or value is None:
            return f"The preset {name} sets {key} to something other than one value."
        if any(fnmatch.fnmatchcase(key.upper(), pattern) for pattern in REFUSED_PRESET_NAMES):
            return f"The preset {name} sets {key}, which a preset may not set."
    return ""


def load_preset(project: Path, name: str) -> dict:
    """The named preset of `service.presets` in the project's pdt.yml, with its env as text.
    Raises Refused when the project has none, or when the preset breaks a rule."""
    try:
        root = config.load_yaml(project / config.PROJECT_FILE)
    except config.ConfigError as e:
        raise Refused(str(e)) from None
    service = root.get("service")
    presets = service.get("presets") if isinstance(service, dict) else None
    if not isinstance(presets, dict) or name not in presets:
        raise Refused(f"{project / config.PROJECT_FILE} has no service preset named {name}.")
    problem = preset_problem(name, presets[name])
    if problem:
        raise Refused(problem)
    preset = presets[name]
    return {"env": {key: str(value) for key, value in preset.get("env", {}).items()},
            "artifacts": preset.get("artifacts", False)}
