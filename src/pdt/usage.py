"""Anonymous usage statistics: what pdt tells its maintainers about how it is used.

One request, which `_post` sends to USAGE_URL (PDT_USAGE_URL overrides it), is
{"version": 1, "events": [row, ...]}. Each row is:

    name          an event of lifecycle.EVENTS, or lifecycle.CLI_COMMAND
    timestamp     the UTC time of the event
    client_uuid   this user on this computer
    project_uuid  the project, or null when there is no project
    command_uuid  one top-level command and the processes it starts (COMMAND_ENV)
    pdt_version, python_version, os, architecture, ci
    params        the event's params that KNOWN_VALUES or PARAM_RULES allow

A param outside these rules is dropped, so an app that emits a pdt event name cannot send
free text in a param that has known values. `app` and `command_line` stay free text.

`command_line` is the typed text after its exclusion rules. `exception_fields` is
the type name and the pdt file and line of an exception, with no message.

`accept` is the usage row of lifecycle.SINKS and returns at once. The first event
starts one daemon thread. The thread sends all queued events as one request, at once
and with no retry, so a process that dies keeps what it already sent. `wait` waits
for unfinished sends. Every send failure is silent.

Under the data folder (`folder`), client.json is {"client_uuid": str | null, "asked": bool}:

    no file                                 no answer yet
    {"client_uuid": "...", "asked": true}   the person said yes
    {"client_uuid": null,  "asked": true}   the person said no
    {"client_uuid": "...", "asked": false}  pdt sent with no one to ask (CI, an agent)

projects/<sha256 of the project path> holds the project's UUID. The name stays on
this computer, and no hash is ever sent.

`start` asks a person once, before the command runs. A person who has not answered,
at a terminal in a command that skips the question, sends
nothing yet. With no one to ask, pdt sends until someone opts out. Any one of
DO_NOT_TRACK, OFF_ENV with an off value, or `anonymous_usage_stats: false` in pdt.yml
turns it off, with no output.

A scheduled job, or any process that has JOB_ENV (`as_job` sets it for the job and its
child processes) or CLIENT_ENV, never asks and never writes a file. It sends only when
`job_env`, which a deploy puts in the job's environment, gave it OFF_ENV, CLIENT_ENV,
and PROJECT_ENV.

This module is in deploy_common.LIFECYCLE_MODULES. It imports only the standard
library and pdt.config, and a job never reaches its imports of pdt.console and pdt.watch.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import math
import os
import re
import shlex
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from pdt import config

USAGE_URL = "https://usage.pdt.autoidm.com/v1/usage"
URL_ENV = "PDT_USAGE_URL"
OFF_ENV = "PDT_ANONYMOUS_USAGE_STATS"
OFF_VALUES = ("0", "false", "off", "no")
CLIENT_ENV = "PDT_USAGE_CLIENT_UUID"
PROJECT_ENV = "PDT_USAGE_PROJECT_UUID"
COMMAND_ENV = "PDT_COMMAND_UUID"
JOB_ENV = "PDT_USAGE_JOB"
SEND_TIMEOUT_SECONDS = 2.0
EXIT_WAIT_SECONDS = 5.0
QUESTION = "Send anonymous usage statistics to AutoIDM?"
OPT_OUT_FLAG = "--no-usage-stats"
NO_QUESTION_FLAGS = ("--help", "--version", "--json", "--yes", "--plan", "--names", "--script",
                     OPT_OUT_FLAG)
NO_QUESTION_SHORT_FLAGS = ("-h", "-y")
KNOWN_VALUES: dict[str, tuple[str, ...]] = {
    "provider": config.PROVIDERS,
    "trigger": ("manual", "schedule"),
    "reason": ("declined", "interrupted", "unauthorized"),
    "signal": ("SIGINT", "SIGTERM"),
    "action": config.SECRET_ACTIONS,
}

EXCLUDED = "<excluded>"
MAX_TEXT = 128
MAX_FOLDER_NAME = 255
MAX_COMMAND_LINE = 1024
MAX_INT = 2**63
UUID_SHAPE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
SECRET_FLAG = re.compile(r"code|pass|secret|token|key|credential|value|connection|sas",
                         re.IGNORECASE)
OWN_SECRET_FLAGS = ("--code",)
CLOUD_LOGIN_FLAGS = ("-u", "-p", "--username")
CODE_LETTER = "[ABCDEFGHJKMNPQRSTVWXYZ23456789]"
SECRET_VALUE = re.compile(
    rf"(?<![A-Z0-9]){CODE_LETTER}{{4}}-{CODE_LETTER}{{4}}(?![A-Z0-9])"
    r"|eyJ[\w-]+\.[\w-]+\.[\w-]*"
    r"|(?<![A-Z0-9])(?:AKIA|ASIA)[A-Z0-9]{16}(?![A-Z0-9])"
    r"|[A-Za-z][A-Za-z0-9+.-]*://[^/\s:@]*:[^/\s@]+@")
ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
APP_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


@dataclass(frozen=True)
class Identity:
    client_uuid: str
    project_uuid: str | None
    command_uuid: str


_resolving = threading.Lock()
_resolved = False
_identity: Identity | None = None
# One lock for the queue and the sender's state; a Condition, as lifecycle's _lock is.
_changed = threading.Condition()
_queued: list[dict] = []
_sending = False
_sender: threading.Thread | None = None
_gave_up = False
_waited_seconds = 0.0


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and abs(value) < MAX_INT


def _is_exception(value: object) -> bool:
    return (isinstance(value, dict) and set(value) == {"type", "file", "line"}
            and isinstance(value["type"], str) and len(value["type"]) <= MAX_TEXT
            and (value["file"] is None
                 or (isinstance(value["file"], str) and len(value["file"]) <= MAX_TEXT))
            and (value["line"] is None or _is_int(value["line"])))


PARAM_RULES = {
    "deployed": lambda value: isinstance(value, bool),
    "exit_code": _is_int,
    "count": _is_int,
    "duration_seconds": lambda value: (_is_int(value)
                                       or (isinstance(value, float) and math.isfinite(value))),
    "app": lambda value: isinstance(value, str) and len(value) <= MAX_FOLDER_NAME,
    "command_line": lambda value: isinstance(value, str) and len(value) <= MAX_COMMAND_LINE,
    "exception": _is_exception,
}


def _is_uuid(value: object) -> bool:
    return isinstance(value, str) and UUID_SHAPE.fullmatch(value) is not None


def folder() -> Path:
    return config.data_home() / "pdt" / "usage"


def _env_off() -> bool:
    return (os.environ.get("DO_NOT_TRACK", "").strip() not in ("", "0")
            or os.environ.get(OFF_ENV, "").strip().lower() in OFF_VALUES)


def off() -> bool:
    if _env_off():
        return True
    try:
        root = config.load_yaml(config.find_project() / config.PROJECT_FILE)
        return root.get(config.USAGE_STATS_KEY) is False
    except (config.ConfigError, OSError, UnicodeDecodeError):
        return False


def _replace(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{uuid.uuid4().hex}")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def _create(path: Path, text: str) -> bool:
    """Write a new file, whole, and return False when the file exists."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{uuid.uuid4().hex}")
    temporary.write_text(text, encoding="utf-8")
    try:
        os.link(temporary, path)
        return True
    except FileExistsError:
        return False
    finally:
        temporary.unlink(missing_ok=True)


def _read_client() -> dict | None:
    try:
        client = json.loads((folder() / "client.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return None
    if not isinstance(client, dict) or not isinstance(client.get("asked"), bool):
        return None
    client_uuid = client.get("client_uuid", "")
    if _is_uuid(client_uuid) or (client_uuid is None and client["asked"]):
        return client
    return None


def _project_uuid() -> str | None:
    try:
        project = config.find_project()
    except config.ConfigError:
        return None
    path = folder() / "projects" / hashlib.sha256(str(project).encode()).hexdigest()
    try:
        project_uuid = path.read_text(encoding="utf-8").strip()
    except (OSError, ValueError):
        project_uuid = ""
    if not _is_uuid(project_uuid):
        project_uuid = str(uuid.uuid4())
        _replace(path, project_uuid)
    return project_uuid


def _command_uuid() -> str:
    if not _is_uuid(os.environ.get(COMMAND_ENV)):
        os.environ[COMMAND_ENV] = str(uuid.uuid4())
    return os.environ[COMMAND_ENV]


def _resolve() -> Identity | None:
    if off():
        return None
    if JOB_ENV in os.environ or CLIENT_ENV in os.environ:
        client = os.environ.get(CLIENT_ENV)
        project = os.environ.get(PROJECT_ENV)
        if not _is_uuid(client) or not _is_uuid(project) or OFF_ENV not in os.environ:
            return None
        return Identity(client, project, _command_uuid())
    record = _read_client()
    if record is None or not record["asked"]:
        from pdt import watch
        if watch.person_at_terminal():
            return None
    if record is None:
        record = {"client_uuid": str(uuid.uuid4()), "asked": False}
        try:
            if not _create(folder() / "client.json", json.dumps(record)):
                saved = _read_client()
                if saved is None:
                    _replace(folder() / "client.json", json.dumps(record))
                else:
                    record = saved
        except OSError:
            return None
    if record["client_uuid"] is None:
        return None
    try:
        project = _project_uuid()
    except OSError:
        return None
    return Identity(record["client_uuid"], project, _command_uuid())


def identity() -> Identity | None:
    """Who this process sends as, or None when it sends nothing; found once in a process."""
    global _resolved, _identity
    with _resolving:
        if not _resolved:
            _identity = _resolve()
            _resolved = True
        return _identity


def as_job() -> None:
    os.environ[JOB_ENV] = "true"


def _named(token: str, flag: str) -> bool:
    """True for `flag` and for what argparse takes as short for it, such as `--js`."""
    return len(token) > 2 and flag.startswith(token)


def start(argv: list[str]) -> None:
    """Before a top-level command: ask once, and name the command for its child processes."""
    try:
        from pdt import console, watch
        tokens = argv[1:]
        if any(_named(token, OPT_OUT_FLAG) for token in tokens):
            os.environ[OFF_ENV] = "false"
        record = _read_client() or {}
        skipped = any(token in NO_QUESTION_SHORT_FLAGS or _named(token, flag)
                      for token in tokens for flag in NO_QUESTION_FLAGS)
        if (not skipped and not record.get("asked") and JOB_ENV not in os.environ
                and CLIENT_ENV not in os.environ and not off() and watch.person_at_terminal()):
            send = console.confirm(QUESTION, default=True, stderr=True)
            client_uuid = (record.get("client_uuid") or str(uuid.uuid4())) if send else None
            _replace(folder() / "client.json",
                     json.dumps({"client_uuid": client_uuid, "asked": True}))
        identity()
    except Exception:
        pass


def job_env() -> dict[str, str]:
    """The env vars a deploy puts in a job, so the job sends as the user who deployed it."""
    try:
        from pdt import __version__
        found = identity()
        if found is not None and found.project_uuid is not None:
            return {OFF_ENV: "true", CLIENT_ENV: found.client_uuid,
                    PROJECT_ENV: found.project_uuid, "PDT_CLI_VERSION": __version__}
    except Exception:
        pass
    return {OFF_ENV: "false"}


def _allowed(key: str, value: object) -> bool:
    if key in KNOWN_VALUES:
        return value in KNOWN_VALUES[key]
    return key in PARAM_RULES and PARAM_RULES[key](value)


def _row(event, who: Identity) -> dict:
    from pdt import __version__
    return {
        "name": event.name,
        "timestamp": event.at.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "client_uuid": who.client_uuid,
        "project_uuid": who.project_uuid,
        "command_uuid": who.command_uuid,
        "pdt_version": __version__[:MAX_TEXT],
        "python_version": ".".join(map(str, sys.version_info[:3]))[:MAX_TEXT],
        "os": sys.platform[:MAX_TEXT],
        "architecture": (os.uname().machine if hasattr(os, "uname")
                         else os.environ.get("PROCESSOR_ARCHITECTURE", ""))[:MAX_TEXT],
        "ci": os.environ.get("CI", "").strip() != "",
        "params": {key: value for key, value in event.params.items() if _allowed(key, value)},
    }


def accept(event) -> None:
    global _sender, _gave_up
    try:
        opted_out_after_start = _env_off()
        who = None if opted_out_after_start else identity()
        if who is None:
            return
        row = _row(event, who)
        with _changed:
            if _gave_up:
                return
            _queued.append(row)
            if _sender is None:
                sender = threading.Thread(target=_send_each, daemon=True)
                sender.start()
                _sender = sender
            _changed.notify_all()
    except Exception:
        with _changed:
            _gave_up = True
            _queued.clear()


def _post(rows: list[dict]) -> None:
    import urllib.request
    request = urllib.request.Request(
        os.environ.get(URL_ENV) or USAGE_URL,
        data=json.dumps({"version": 1, "events": rows}).encode(),
        headers={"content-type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=SEND_TIMEOUT_SECONDS) as response:
        response.read()


def _send_each() -> None:
    global _sending
    while True:
        with _changed:
            _changed.wait_for(lambda: _queued)
            rows = list(_queued)
            _queued.clear()
            _sending = True
        try:
            _post(rows)
        except Exception:
            pass
        with _changed:
            _sending = False
            _changed.notify_all()


def wait(at_most: float | None = None) -> None:
    global _gave_up, _waited_seconds
    with _changed:
        if _gave_up or (not _queued and not _sending):
            return
        limit = EXIT_WAIT_SECONDS - _waited_seconds
        if at_most is not None:
            limit = min(limit, at_most)
        began = time.monotonic()
        finished = _changed.wait_for(lambda: not _queued and not _sending, limit)
        _waited_seconds += time.monotonic() - began
        if not finished:
            _gave_up = True
            _queued.clear()


def exception_fields(error: BaseException) -> dict[str, object]:
    root = Path(__file__).resolve().parent
    file, line = None, None
    frame = error.__traceback__
    while frame is not None:
        path = Path(frame.tb_frame.f_code.co_filename).resolve()
        if path.is_relative_to(root):
            file, line = f"pdt/{path.relative_to(root).as_posix()}", frame.tb_lineno
        frame = frame.tb_next
    return {"type": type(error).__name__, "file": file, "line": line}


def _command_rules(words: list[str]) -> list[str]:
    """Rules c and d of command_line. A close spelling counts, so a typo keeps them."""
    positional = [word for word in words if not word.startswith("-")]
    if not positional:
        return words
    command = difflib.get_close_matches(positional[0], ("secrets", "storage"), n=1)
    after = words.index(positional[0]) + 1
    if command == ["storage"]:
        for word in positional[1:3]:
            if difflib.get_close_matches(word, ("query",), n=1):
                sql = words.index(word, after) + 1
                return words[:sql] + [EXCLUDED] if words[sql:] else words
    if command == ["secrets"]:
        return words[:after] + _exclude_from_first_uncertain_role(words[after:])
    return words


def _exclude_from_first_uncertain_role(words: list[str]) -> list[str]:
    kept: list[str] = []
    roles: list[str] | None = None
    action = ""
    for index, word in enumerate(words):
        if word in NO_QUESTION_SHORT_FLAGS or _named(word, "--yes") or _named(word, "--help"):
            kept.append(word)
            continue
        if roles is None:
            roles = ["action", "name"] if word in config.SECRET_ACTIONS else ["app", "action", "name"]
        role = roles.pop(0) if roles else ""
        if role == "action" and word in config.SECRET_ACTIONS:
            action = word
        elif not ((role == "app" and APP_WORD.fullmatch(word))
                  or (role == "name" and action == "set" and ENV_NAME.fullmatch(word))):
            return kept + [EXCLUDED] * (len(words) - index)
        kept.append(word)
    return kept


def command_line(argv: list[str], home: str | None = None) -> str:
    """The typed command after the exclusion rules, which need no parse, so they also hold
    after a parse error:

    a. A flag name that holds a SECRET_FLAG word, or that argparse takes as short for an
       OWN_SECRET_FLAGS flag, loses its value, in both flag forms.
    b. CLOUD_LOGIN_FLAGS lose their values, also a value attached to -u or -p. No pdt command
       has its own -u or -p, so this holds in every command, and a mistyped `pdt az` keeps it
       too.
    c. The SQL of `pdt storage ... query` goes.
    d. `pdt secrets` keeps a word only when it is certainly the app, the action, or the name.
    e. A word that holds a SECRET_VALUE shape goes.
    f. The home folder becomes ~, in any case and with either separator when it is a Windows
       folder, and the text has at most MAX_COMMAND_LINE characters.
    """
    kept = []
    takes_value = False
    for word in _command_rules(argv[1:]):
        name, equals, value = word.partition("=")
        flag = word.startswith("-") and word.strip("-") != ""
        if takes_value:
            kept.append(EXCLUDED)
            takes_value = False
        elif word == EXCLUDED:
            kept.append(word)
        elif flag and re.match(r"-[up].", word):
            kept.append(f"{word[:2]}{EXCLUDED}")
            takes_value = not equals and SECRET_FLAG.search(name) is not None
        elif flag and (SECRET_FLAG.search(name) or name in CLOUD_LOGIN_FLAGS
                       or any(_named(name, secret) for secret in OWN_SECRET_FLAGS)):
            kept.append(f"{name}={EXCLUDED}" if equals else word)
            takes_value = not equals
        elif flag and equals and SECRET_VALUE.search(value):
            kept.append(f"{name}={EXCLUDED}")
        elif SECRET_VALUE.search(word):
            kept.append(EXCLUDED)
        else:
            kept.append(word)
    home_folder = (str(Path.home()) if home is None else home).rstrip("/\\")
    if home_folder != "":
        windows = re.match(r"[A-Za-z]:|\\", home_folder) is not None
        parts = re.split(r"[/\\]+", home_folder) if windows else [home_folder]
        at_home = re.compile(r"[/\\]+".join(map(re.escape, parts)) + r"(?=$|[/\\])",
                             re.IGNORECASE if windows else 0)
        kept = [at_home.sub("~", word) for word in kept]
    text = " ".join(["pdt", *(shlex.quote(word) if word == "" or re.search(r"\s", word) else word
                              for word in kept)])
    return text[:MAX_COMMAND_LINE]
