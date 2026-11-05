"""The closed list of operations the portal can ask for, and the checks on a signed token.

OPERATIONS maps each operation to its args, the pdt command line it runs, its expiry, and its run
limits. The portal holds the same table. A command line comes only from `Operation.argv`, which
reads args that `args_problem` already passed, so no free text, shell string, or command line in a
token can reach a program. `verify` is the one gate for a command, `verify_cancel` is the one gate
for a cancel, and `next_key_set` is the one gate for new command keys. Each raises Refused with a
plain message. `Clock` holds the difference from AutoIDM's time, and each check uses it.
"""

from __future__ import annotations

import os
import re
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import jwt

from pdt import config

ISSUER = "pdt-portal"
KEY_SET_ISSUER = "pdt-signer"
CANCEL = "cancel"
RESERVED_PROJECT = "service"
COMMAND_ID = re.compile(r"cmd_\d{8}T\d{6}Z_[0-9a-f]{8}")
NAME = re.compile(r"(?!\.{1,2}$)(?!-)[A-Za-z0-9._-]{1,128}")
RUN_NUMBER = re.compile(r"[0-9]{1,6}")
ENV_VAR = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")
ARG_RULES = {"project": NAME, "app": NAME, "run": RUN_NUMBER, "name": ENV_VAR,
             "mr_iid": re.compile(r"[0-9]{1,9}"), "sha": re.compile(r"[0-9a-f]{40}"),
             "timezone": re.compile(r"[A-Za-z][A-Za-z0-9_+/-]{0,63}")}
BOOLEAN_ARGS = ("deployed",)
MAX_APP_ARGS = 1024
PLACE = re.compile(r"(aws|azure|google-cloud):[A-Za-z0-9-]{1,64}:[a-z0-9-]{1,32}")
HOUR = 3600
MINUTE = 60
CANCEL_EXPIRES_SECONDS = 25200
PROJECT_ENV_LOCK = threading.Lock()


class Refused(Exception):
    """A command the machine must not run. The message goes to the portal."""


@dataclass
class Clock:
    offset: float = 0.0

    def now(self) -> float:
        return time.time() + self.offset


def run_argv(args: dict) -> list[str]:
    return ["run", args["app"], *(["--deployed", "--follow"] if args["deployed"] else [])]


def app_option(args: dict) -> list[str]:
    return [args["app"]] if "app" in args else []


@dataclass(frozen=True)
class Operation:
    required: tuple[str, ...]
    optional: tuple[str, ...]
    argv: Callable[[dict], list[str]]
    expires_seconds: int
    limit_seconds: int
    runner_limit_seconds: int | None
    exclusive: bool = False
    from_copy: bool = False
    module: str = "pdt.cli"


OPERATIONS = {
    "list": Operation(("project",), (), lambda a: ["list", "--json"], HOUR, 5 * MINUTE, 5 * MINUTE),
    "validate": Operation(("project",), ("app",), lambda a: ["validate"], HOUR, 5 * MINUTE,
                          5 * MINUTE),
    "runs": Operation(("project", "app"), (), lambda a: ["runs", a["app"], "--json"], HOUR,
                      5 * MINUTE, 5 * MINUTE),
    "logs": Operation(("project", "app"), ("run",),
                      lambda a: ["logs", a["app"], *([a["run"]] if "run" in a else [])],
                      HOUR, 5 * MINUTE, 5 * MINUTE),
    "health": Operation(("project",), (), lambda a: ["health", "--json"], HOUR, 5 * MINUTE,
                        5 * MINUTE),
    "artifacts": Operation(("project", "app", "run"), (), lambda a: [a["app"], a["run"]], HOUR,
                           15 * MINUTE, 15 * MINUTE, module="pdt.run_output"),
    "plan": Operation(("project", "app"), (), lambda a: ["deploy", a["app"], "--plan"], HOUR,
                      15 * MINUTE, 15 * MINUTE),
    "run": Operation(("project", "app", "deployed"), (), run_argv, HOUR, 6 * HOUR, 55 * MINUTE),
    "kill": Operation(("project", "app"), (), lambda a: ["run", a["app"], "--kill"], HOUR,
                      5 * MINUTE, 5 * MINUTE),
    "pause": Operation(("project", "app"), (), lambda a: ["pause", a["app"]], 24 * HOUR,
                       15 * MINUTE, 15 * MINUTE),
    "unpause": Operation(("project", "app"), (), lambda a: ["unpause", a["app"]], 24 * HOUR,
                         15 * MINUTE, 15 * MINUTE),
    "deploy": Operation(("project", "app"), ("timezone",), lambda a: ["deploy", a["app"], "--yes"],
                        24 * HOUR, HOUR, 55 * MINUTE, exclusive=True),
    "destroy": Operation(("project", "app"), (), lambda a: ["destroy", a["app"], "--yes"],
                         15 * MINUTE, HOUR, 55 * MINUTE, exclusive=True),
    "secrets_set": Operation(("project", "app", "name"), (),
                             lambda a: ["secrets", a["app"], "set", a["name"]], 15 * MINUTE,
                             5 * MINUTE, 5 * MINUTE),
    "sync_up": Operation(("project",), ("app",), lambda a: ["sync", "up", *app_option(a), "--yes"],
                         24 * HOUR, 15 * MINUTE, None, exclusive=True),
    "sync_down": Operation(("project",), ("app",),
                           lambda a: ["sync", "down", *app_option(a), "--yes"],
                           24 * HOUR, 15 * MINUTE, None, exclusive=True),
    "run_mr": Operation(("project", "app", "mr_iid", "sha"), ("app_args",),
                        lambda a: ["run", a["app"]], HOUR, 6 * HOUR, 55 * MINUTE, from_copy=True),
}


def args_problem(name: str, args: object) -> str:
    operation = OPERATIONS[name]
    if not isinstance(args, dict):
        return "the args are not an object"
    for arg in args:
        if arg not in (*operation.required, *operation.optional):
            return f"the arg {arg!r} is not one this operation takes"
    for arg in operation.required:
        if arg not in args:
            return f"the arg {arg} is missing"
    for arg, value in args.items():
        if arg in BOOLEAN_ARGS:
            if not isinstance(value, bool):
                return f"the arg {arg} is not true or false"
        elif arg == "app_args":
            if not (isinstance(value, str) and len(value) <= MAX_APP_ARGS
                    and not any(ord(character) < 32 or ord(character) == 127
                                for character in value)):
                return (f"the arg app_args is not text of at most {MAX_APP_ARGS} characters with "
                        "no control character")
        elif not (isinstance(value, str) and ARG_RULES[arg].fullmatch(value)):
            return f"the arg {arg} does not have the form this operation takes"
    if args.get("project") == RESERVED_PROJECT:
        return f"the project name {RESERVED_PROJECT} is reserved"
    return ""


def command_line(operation: str, args: dict) -> list[str]:
    found = OPERATIONS[operation]
    return [sys.executable, "-m", found.module, *found.argv(args)]


def decoded(token: object, keys: list[dict], audience: str | None = None,
            issuer: str = ISSUER) -> dict:
    try:
        kid = jwt.get_unverified_header(token).get("kid")
    except (jwt.InvalidTokenError, TypeError, ValueError, AttributeError):
        raise Refused("The token does not parse.") from None
    pem = next((key.get("pem") for key in keys
                if isinstance(key, dict) and key.get("kid") == kid), None)
    if pem is None:
        raise Refused("A key this computer does not know signed the token.")
    try:
        return jwt.decode(token, pem, algorithms=["ES256"], audience=audience, issuer=issuer,
                          options={"verify_exp": False, "verify_iat": False, "verify_nbf": False})
    except jwt.InvalidAudienceError:
        raise Refused("The token is for another installation.") from None
    except jwt.InvalidTokenError as e:
        raise Refused(f"The token failed its check: {e}.") from None
    except ValueError:
        raise Refused("The pinned key does not parse.") from None


def expired(claims: dict, now: float) -> bool:
    return now >= claims["exp"]


def check_times(claims: dict, now: float, longest: int) -> None:
    exp, iat = claims.get("exp"), claims.get("iat")
    if not (type(exp) is int and type(iat) is int):
        raise Refused("The token has no valid times.")
    if exp - iat > longest:
        raise Refused(f"The token is valid for {exp - iat} seconds, and the most it can be is "
                      f"{longest} seconds.")
    if expired(claims, now):
        raise Refused("The token expired.")


def verify(token: str, command_id: str, installation_id: str, customer_id: str, keys: list[dict],
           now: float) -> dict:
    if not COMMAND_ID.fullmatch(str(command_id)):
        raise Refused("The command ID does not have the form pdt expects.")
    claims = decoded(token, keys, installation_id)
    if claims.get("sub") != command_id:
        raise Refused("The command token names another command.")
    if claims.get("customer_id") != customer_id:
        raise Refused("The command is for another customer.")
    name = claims.get("operation")
    if not isinstance(name, str) or name not in OPERATIONS:
        raise Refused(f"pdt does not know the operation {name!r}.")
    operation = OPERATIONS[name]
    check_times(claims, now, operation.expires_seconds)
    problem = args_problem(name, claims.get("args"))
    if problem:
        raise Refused(f"The {name} command was refused: {problem}.")
    limit = claims.get("limit_seconds")
    if not (type(limit) is int and 0 < limit):
        raise Refused("The command has no run limit.")
    claims["limit_seconds"] = min(limit, operation.limit_seconds)
    return claims


def verify_cancel(token: object, command_id: str, installation_id: str, customer_id: str,
                  keys: list[dict], now: float) -> None:
    claims = decoded(token, keys, installation_id)
    if claims.get("sub") != command_id or claims.get("customer_id") != customer_id:
        raise Refused("The cancel token names another command.")
    if claims.get("operation") != CANCEL:
        raise Refused("The token is not a cancel token.")
    check_times(claims, now, CANCEL_EXPIRES_SECONDS)


def next_key_set(token: object, version: int, keys: list[dict],
                 customer_id: str) -> tuple[int, list[dict]]:
    claims = decoded(token, keys, customer_id, KEY_SET_ISSUER)
    found = claims.get("command_keys_version")
    if found != version + 1 or type(found) is not int:
        raise Refused(f"The key set has the version {found!r}, and this computer holds {version}.")
    new = claims.get("keys")
    if not (isinstance(new, list) and new
            and all(isinstance(key, dict) and isinstance(key.get("kid"), str)
                    and isinstance(key.get("pem"), str) for key in new)):
        raise Refused(f"The key set of version {found} holds no valid keys.")
    return found, new


@contextmanager
def in_project(project: Path):
    with PROJECT_ENV_LOCK:
        before = os.environ.get("PDT_PROJECT")
        os.environ["PDT_PROJECT"] = str(project)
        try:
            yield
        finally:
            if before is None:
                del os.environ["PDT_PROJECT"]
            else:
                os.environ["PDT_PROJECT"] = before


def env_spec_names(project: Path, app: str) -> set[str]:
    with in_project(project):
        spec = config.env_spec(config.merged_app(app))
    names = {*(spec.get("required") or []), *(spec.get("optional") or [])}
    for group in spec.get("one_of") or []:
        names.update(group)
    return {str(name) for name in names}


