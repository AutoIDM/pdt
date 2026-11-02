"""Anonymous usage statistics: what pdt tells its maintainers about how it is used.

One request, which `_post` sends to USAGE_URL (PDT_USAGE_URL overrides it), is
{"version": 1, "events": [row, ...]}. Each row is:

    name          an event of lifecycle.EVENTS, or lifecycle.CLI_COMMAND
    timestamp     the UTC time of the event
    client_uuid   this user on this computer
    project_uuid  the project, or null when there is no project
    command_uuid  one top-level command and the processes it starts (COMMAND_ENV)
    pdt_version, python_version, os, architecture, ci
    params        the event's params that ALLOWED_PARAMS names

`command_line` is the typed text after its exclusion rules. `exception_fields` is
the type name and the pdt file and line of an exception, with no message.

`accept` is the usage row of lifecycle.SINKS and returns at once. The first event
starts one daemon thread. The thread sends all queued events as one request, at once
and with no retry, so a process that dies keeps what it already sent. `wait` waits
for unfinished sends, at most EXIT_WAIT_SECONDS for the whole process. Every send
failure is silent.

Under the data folder (`folder`), client.json is {"client_uuid": str | null, "asked": bool}:

    no file                                 no answer yet
    {"client_uuid": "...", "asked": true}   the person said yes
    {"client_uuid": null,  "asked": true}   the person said no
    {"client_uuid": "...", "asked": false}  pdt sent with no one to ask (CI, an agent)

projects/<sha256 of the project path> holds the project's UUID. The name stays on
this computer, and no hash is ever sent.

`start` asks a person once, before the command runs. A person who has not answered,
at a terminal in a command that skips the question, sends nothing yet. With no one to
ask, pdt sends until someone opts out. Any one of DO_NOT_TRACK, OFF_ENV with an off
value, or `anonymous_usage_stats: false` in pdt.yml turns it off, with no output.

A scheduled job (`as_job`, or any process that has CLIENT_ENV) never asks and never
writes a file. It sends only when `job_env`, which a deploy puts in the job's
environment, gave it OFF_ENV, CLIENT_ENV, and PROJECT_ENV.

This module is in deploy_common.LIFECYCLE_MODULES. It imports only the standard
library and pdt.config, and a job never reaches its imports of pdt.console and pdt.watch.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import shlex
import sys
import threading
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
SEND_TIMEOUT_SECONDS = 2.0
EXIT_WAIT_SECONDS = 5.0
QUESTION = "Send anonymous usage statistics to AutoIDM?"
OPT_OUT_FLAG = "--no-usage-stats"
NO_QUESTION_FLAGS = ("--help", "--version", "--json", "--yes", OPT_OUT_FLAG)
NO_QUESTION_SHORT_FLAGS = ("-h", "-y")
ALLOWED_PARAMS = ("provider", "deployed", "trigger", "exit_code", "reason", "signal", "action",
                  "count", "duration_seconds", "app", "command_line", "exception")

EXCLUDED = "<excluded>"
MAX_COMMAND_LINE = 1024
SECRET_FLAG = re.compile(r"code|pass|secret|token|key|credential|value|connection|sas",
                         re.IGNORECASE)
CLOUD_LOGIN_FLAGS = ("-u", "-p", "--username")
CODE_LETTER = "[ABCDEFGHJKMNPQRSTVWXYZ23456789]"
SECRET_VALUE = re.compile(
    rf"(?<![A-Z0-9]){CODE_LETTER}{{4}}-{CODE_LETTER}{{4}}(?![A-Z0-9])"
    r"|eyJ[\w-]+\.[\w-]+\.[\w-]*"
    r"|(?<![A-Z0-9])(?:AKIA|ASIA)[A-Z0-9]{16}(?![A-Z0-9])"
    r"|[A-Za-z][A-Za-z0-9+.-]*://[^/\s:@]*:[^/\s@]+@")
ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


@dataclass(frozen=True)
class Identity:
    client_uuid: str
    project_uuid: str | None
    command_uuid: str


_job = False
_resolved = False
_identity: Identity | None = None
# One lock for the queue and the sender's state; a Condition, as lifecycle's _lock is.
_changed = threading.Condition()
_queued: list[dict] = []
_sending = False
_sender: threading.Thread | None = None
_gave_up = False


def folder() -> Path:
    return config.data_home() / "pdt" / "usage"


def off() -> bool:
    if os.environ.get("DO_NOT_TRACK", "").strip() not in ("", "0"):
        return True
    if os.environ.get(OFF_ENV, "").strip().lower() in OFF_VALUES:
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


def _read_client() -> dict | None:
    try:
        client = json.loads((folder() / "client.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return client if isinstance(client, dict) else None


def _project_uuid() -> str | None:
    try:
        project = config.find_project()
    except config.ConfigError:
        return None
    path = folder() / "projects" / hashlib.sha256(str(project).encode()).hexdigest()
    if not path.is_file():
        _replace(path, str(uuid.uuid4()))
    return path.read_text(encoding="utf-8").strip()


def _resolve() -> Identity | None:
    if off():
        return None
    if _job or CLIENT_ENV in os.environ:
        client = os.environ.get(CLIENT_ENV, "")
        project = os.environ.get(PROJECT_ENV, "")
        if client == "" or project == "" or OFF_ENV not in os.environ:
            return None
        return Identity(client, project, os.environ.setdefault(COMMAND_ENV, str(uuid.uuid4())))
    record = _read_client()
    if record is None or not record.get("asked"):
        from pdt import watch
        if watch.can_offer():
            return None
    if record is None:
        record = {"client_uuid": str(uuid.uuid4()), "asked": False}
        try:
            _replace(folder() / "client.json", json.dumps(record))
        except OSError:
            return None
    if not record.get("client_uuid"):
        return None
    try:
        project = _project_uuid()
    except OSError:
        return None
    return Identity(record["client_uuid"], project,
                    os.environ.setdefault(COMMAND_ENV, str(uuid.uuid4())))


def identity() -> Identity | None:
    """Who this process sends as, or None when it sends nothing; found once in a process."""
    global _resolved, _identity
    with _changed:
        if not _resolved:
            _identity = _resolve()
            _resolved = True
        return _identity


def as_job() -> None:
    global _job
    _job = True


def _named(token: str, flag: str) -> bool:
    """True for `flag` and for what argparse takes as short for it, such as `--js`."""
    return len(token) > 2 and flag.startswith(token)


def start(argv: list[str]) -> None:
    """Before a top-level command: ask once, and name the command for its child processes."""
    from pdt import console, watch
    tokens = argv[1:]
    if any(_named(token, OPT_OUT_FLAG) for token in tokens):
        os.environ[OFF_ENV] = "false"
    record = _read_client() or {}
    skipped = any(token in NO_QUESTION_SHORT_FLAGS or _named(token, flag)
                  for token in tokens for flag in NO_QUESTION_FLAGS)
    if (not skipped and not record.get("asked") and CLIENT_ENV not in os.environ
            and not off() and watch.can_offer()):
        try:
            send = console.confirm(QUESTION, default=True)
            client_uuid = (record.get("client_uuid") or str(uuid.uuid4())) if send else None
            _replace(folder() / "client.json",
                     json.dumps({"client_uuid": client_uuid, "asked": True}))
        except (EOFError, OSError):
            pass
    identity()


def job_env() -> dict[str, str]:
    """The env vars a deploy puts in a job, so the job sends as the user who deployed it."""
    from pdt import __version__
    found = identity()
    if found is None or found.project_uuid is None:
        return {OFF_ENV: "false"}
    return {OFF_ENV: "true", CLIENT_ENV: found.client_uuid, PROJECT_ENV: found.project_uuid,
            "PDT_CLI_VERSION": __version__}


def _row(event, who: Identity) -> dict:
    from pdt import __version__
    return {
        "name": event.name,
        "timestamp": event.at.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "client_uuid": who.client_uuid,
        "project_uuid": who.project_uuid,
        "command_uuid": who.command_uuid,
        "pdt_version": __version__,
        "python_version": ".".join(map(str, sys.version_info[:3])),
        "os": sys.platform,
        "architecture": (os.uname().machine if hasattr(os, "uname")
                         else os.environ.get("PROCESSOR_ARCHITECTURE", "")),
        "ci": os.environ.get("CI", "").strip() != "",
        "params": {key: value for key, value in event.params.items() if key in ALLOWED_PARAMS},
    }


def accept(event) -> None:
    global _sender
    who = identity()
    if who is None:
        return
    with _changed:
        if _gave_up:
            return
        _queued.append(_row(event, who))
        if _sender is None:
            _sender = threading.Thread(target=_send_each, daemon=True)
            _sender.start()
        _changed.notify_all()


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


def wait(seconds: float | None = None) -> None:
    global _gave_up
    limit = EXIT_WAIT_SECONDS if seconds is None else seconds
    with _changed:
        if not _gave_up and not _changed.wait_for(lambda: not _queued and not _sending, limit):
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
        action_first = positional[1:2] != [] and positional[1] in config.SECRET_ACTIONS
        roles = iter(("action", "name") if action_first else ("app", "action", "name"))
        kept = words[:after]
        for word in words[after:]:
            if word.startswith("-"):
                name, equals, _value = word.partition("=")
                kept.append(f"{name}={EXCLUDED}" if equals else word)
                continue
            role = next(roles, "")
            if (role == "app" or (role == "action" and word in config.SECRET_ACTIONS)
                    or (role == "name" and ENV_NAME.fullmatch(word))):
                kept.append(word)
            else:
                kept.append(EXCLUDED)
        return kept
    return words


def command_line(argv: list[str], home: str | None = None) -> str:
    """The typed command after the exclusion rules, which need no parse, so they also hold
    after a parse error:

    a. A flag name that holds a SECRET_FLAG word loses its value, in both flag forms.
    b. CLOUD_LOGIN_FLAGS lose their values. No pdt command has its own -u or -p, so this
       holds in every command, and a mistyped `pdt az` keeps it too.
    c. The SQL of `pdt storage ... query` goes.
    d. `pdt secrets` keeps the app, the action, and the name, and no further word.
    e. A word that holds a SECRET_VALUE shape goes.
    f. The home folder becomes ~, and the text has at most MAX_COMMAND_LINE characters.
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
        elif flag and (SECRET_FLAG.search(name) or name in CLOUD_LOGIN_FLAGS):
            kept.append(f"{name}={EXCLUDED}" if equals else word)
            takes_value = not equals
        elif flag and re.match(r"-[up].", word):
            kept.append(f"{word[:2]}{EXCLUDED}")
        elif flag and equals and SECRET_VALUE.search(value):
            kept.append(f"{name}={EXCLUDED}")
        elif SECRET_VALUE.search(word):
            kept.append(EXCLUDED)
        else:
            kept.append(word)
    home_folder = (str(Path.home()) if home is None else home).rstrip("/\\")
    if home_folder != "":
        at_home = re.compile(re.escape(home_folder) + r"(?=$|[/\\])")
        kept = [at_home.sub("~", word) for word in kept]
    text = " ".join(["pdt", *(shlex.quote(word) if word == "" or re.search(r"\s", word) else word
                              for word in kept)])
    return text[:MAX_COMMAND_LINE]
