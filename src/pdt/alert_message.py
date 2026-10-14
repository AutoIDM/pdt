"""What every chat alerts type says: the content of a lifecycle event, before a transport lays it out.

`event_message` and `ready_message` build a Message: a title, a tone, an optional
note, body text, labelled fields, an "Open" link, and a footer. pdt.slack turns a
Message into Block Kit and pdt.teams turns it into an Adaptive Card. The note,
text, footer, and field values use one markup, `*bold*` and `code`, which Slack
shows as written and Teams converts. A Tone (`OUTCOMES`, `OTHER`, `UNHANDLED`,
`READY`) holds the colour and icon of an outcome for both types. The title also
says the outcome in words, so the tone is never the only sign of it.

The secret lookup (`secret_value`), the env var name (`env_name`), and the HTTP
POST (`post`) are shared too. lifecycle.py runs before the app and loads no env,
so `secret_value` finds a webhook URL itself: the environment, then the job's
PDT_ENV_JSON secret, then the `.env` files from the working folder and the
project up.

This module is in deploy_common.LIFECYCLE_MODULES, so it imports only what
lifecycle.py's script header installs.
"""

from __future__ import annotations

import json
import os
import re
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values

from pdt import config

TIMEOUT_SECONDS = 10


@dataclass(frozen=True)
class Tone:
    colour: str
    slack_emoji: str
    emoji: str
    # An Adaptive Card ContainerStyle: good, attention, accent, or emphasis.
    style: str


@dataclass(frozen=True)
class Message:
    title: str
    summary: str
    tone: Tone
    note: str = ""
    text: str = ""
    fields: tuple[tuple[str, str], ...] = ()
    url: str = ""
    footer: str = ""


# By the last word of an event name.
OUTCOMES = {
    "completed": Tone("#2EB67D", ":large_green_circle:", "\U0001F7E2", "good"),
    "failed": Tone("#E01E5A", ":red_circle:", "\U0001F534", "attention"),
}
OTHER = Tone("#1D9BD1", ":large_blue_circle:", "\U0001F535", "accent")
UNHANDLED = Tone("#868686", ":white_circle:", "⚪", "emphasis")
READY = Tone("#2EB67D", ":white_check_mark:", "✅", "good")
# Each parameter's label, in the order the message shows them; any other parameter follows.
FIELDS = {"project": "Project", "app": "App", "provider": "Provider", "deployed": "Where",
          "trigger": "Trigger", "exit_code": "Exit code", "run_id": "Run id", "state": "Logs"}
HIDDEN = {"url"}
# The cloud a deployed run ran in; a deployed Windows task runs on this computer.
CLOUDS = {"aws": "AWS", "azure": "Azure", "google-cloud": "Google Cloud"}
MAX_FIELDS = 10


def env_name(prefix: str, channel: str) -> str:
    """The env var that holds the webhook of `channel`: `SLACK_WEBHOOK_` and #it-alerts give SLACK_WEBHOOK_IT_ALERTS."""
    return prefix + re.sub(r"[^A-Z0-9]+", "_", channel.lstrip("#").upper()).strip("_")


def event_words(name: str) -> str:
    """`job:run-failed` as `Run failed`."""
    words = name.partition(":")[2] or name
    return words.replace("-", " ").replace("_", " ").capitalize()


def event_label(name: str) -> str:
    if name == "all":
        return "all events"
    return name if any(c in name for c in "*?[") else event_words(name)


def event_message(event, unhandled: bool = False) -> Message:
    """The content of one lifecycle event: the same layout for every event."""
    params = event.params
    tone = UNHANDLED if unhandled else OUTCOMES.get(event.name.rsplit("-", 1)[-1], OTHER)
    words = event_words(event.name)
    title = f"{words}: {params['app']}" if "app" in params else words
    # `pdt logs` reads only a deployed run's log, so a local run shows no Logs field.
    keys = [key for key in FIELDS if key in params and (key != "state" or params.get("deployed"))]
    keys += [key for key in params if key not in FIELDS and key not in HIDDEN]
    fields = []
    for key in keys[:MAX_FIELDS]:
        shown = str(params[key])
        if key == "deployed":
            cloud = CLOUDS.get(str(params.get("provider"))) if params[key] else None
            shown = (f"Cloud run on {cloud}" if cloud else
                     f"{'Deployed' if params[key] else 'Local'} run on {socket.gethostname()}")
        elif key == "state":
            shown = f"`pdt logs {params.get('app')} --id {params.get('run_id')}`"
        elif key == "trigger":
            shown = shown.capitalize()
        fields.append((FIELDS.get(key, event_words(key)), shown))
    return Message(
        title=title,
        summary=f"{title} ({params['project']})" if "project" in params else title,
        tone=tone,
        note=(f"*Unhandled event.* No alerts entry lists `{event.name}`, so it came here "
              "through `unhandled`." if unhandled else ""),
        fields=tuple(fields),
        url=str(params["url"]) if "url" in params else "",
        footer=f"`{event.name}` at {event.at:%Y-%m-%d %H:%M} UTC, sent by pdt")


def ready_message(project: str, target: str, events: list[str]) -> Message:
    """The test message. `target` is the destination as the user knows it, such as #it-alerts."""
    names = ", ".join(map(event_label, events)) or "no events yet"
    return Message(
        title="Success, you're ready!", summary="Success, you're ready!", tone=READY,
        text=f"pdt will post here for these events of the project *{project}*: {names}.",
        footer=f"Test message to {target}, sent by `pdt alerts test`")


def secret_value(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if value:
        return value
    try:
        value = str(json.loads(os.environ.get("PDT_ENV_JSON") or "{}").get(name) or "")
    except (ValueError, AttributeError):
        value = ""
    if value.strip():
        return value.strip()
    starts = [Path.cwd()]
    try:
        starts.append(config.find_project())
    except config.ConfigError:
        pass
    for start in starts:
        for path in config.find_env_files(start):
            value = (dotenv_values(path).get(name) or "").strip()
            if value:
                return value
    return ""


def post(url: str, payload: dict, service: str) -> str:
    """POST `payload` to a webhook and return the answer's body. Raises when `service` does not answer 2xx."""
    request = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return response.read().decode(errors="replace").strip()
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace").strip()
        raise RuntimeError(f"{service} answered HTTP {e.code}: {body or e.reason}") from None
    except urllib.error.URLError as e:
        raise RuntimeError(f"{service} could not be reached: {e.reason}") from None
