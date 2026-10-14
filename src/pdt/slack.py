"""The slack alerts type: a Block Kit message in one Slack channel, through an incoming webhook.

An entry is `{type: slack, channel: <name>}` plus its events. The webhook URL is
a secret, so it lives in `.env` as SLACK_WEBHOOK_<CHANNEL> (`secret_name`), and
`config.env_spec` adds that name to every app's env spec, so `pdt deploy` sends
it in the app's cloud secret. A Slack webhook posts to the one channel it was
made for; `channel` names that channel and the env var, and a new channel
needs a new webhook from `pdt alerts setup`.

lifecycle.py runs before the app and loads no env, so `webhook_url` finds the
URL itself: the environment, then the job's PDT_ENV_JSON secret, then the
`.env` files from the working folder and the project up. The message name,
icon, and colour bar come from the Slack app that made the webhook and from
`OUTCOMES`; a webhook cannot set its own name or icon.

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
from pathlib import Path

from dotenv import dotenv_values

from pdt import config

KEYS = frozenset({"channel"})
TIMEOUT_SECONDS = 10
# Slack, and Slack for government (GovSlack).
WEBHOOK_HOSTS = ("hooks.slack.com", "hooks.slack-gov.com")
# (colour bar, emoji) by the last word of an event name. Each title also says the
# outcome in words, so the colour is never the only sign of it.
OUTCOMES = {
    "completed": ("#2EB67D", ":large_green_circle:"),
    "failed": ("#E01E5A", ":red_circle:"),
}
OTHER = ("#1D9BD1", ":large_blue_circle:")
UNHANDLED = ("#868686", ":white_circle:")
READY = ("#2EB67D", ":white_check_mark:")
# Each parameter's label, in the order the message shows them; any other parameter follows.
FIELDS = {"project": "Project", "app": "App", "provider": "Provider", "deployed": "Where",
          "trigger": "Trigger", "exit_code": "Exit code", "run_id": "Run id", "state": "Logs"}
HIDDEN = {"url"}
# The cloud a deployed run ran in; a deployed Windows task runs on this computer.
CLOUDS = {"aws": "AWS", "azure": "Azure", "google-cloud": "Google Cloud"}
MAX_FIELDS = 10


def secret_name(channel: str) -> str:
    """The env var that holds the webhook of `channel`: #it-alerts is SLACK_WEBHOOK_IT_ALERTS."""
    return "SLACK_WEBHOOK_" + re.sub(r"[^A-Z0-9]+", "_", channel.lstrip("#").upper()).strip("_")


def secrets(entry: dict) -> list[str]:
    return [secret_name(str(entry["channel"]))]


def event_words(name: str) -> str:
    """`job:run-failed` as `Run failed`."""
    words = name.partition(":")[2] or name
    return words.replace("-", " ").replace("_", " ").capitalize()


def message(event, unhandled: bool = False) -> dict:
    """The Block Kit payload of one lifecycle event: the same layout for every event."""
    params = event.params
    words = event_words(event.name)
    colour, emoji = UNHANDLED if unhandled else OUTCOMES.get(
        event.name.rsplit("-", 1)[-1], OTHER)
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
        fields.append({"type": "mrkdwn", "text": f"*{FIELDS.get(key, event_words(key))}*\n{shown}"})
    blocks: list[dict] = [{"type": "header",
                           "text": {"type": "plain_text", "text": f"{emoji} {title}"}}]
    if unhandled:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": (
            f"*Unhandled event.* No alerts entry lists `{event.name}`, so it came here "
            "through `unhandled`.")}]})
    if fields:
        blocks.append({"type": "section", "fields": fields})
    if "url" in params:
        blocks.append({"type": "actions", "elements": [{
            "type": "button", "text": {"type": "plain_text", "text": "Open"},
            "url": str(params["url"])}]})
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": (
        f"`{event.name}` at {event.at:%Y-%m-%d %H:%M} UTC, sent by pdt")}]})
    return {"text": f"{title} ({params['project']})" if "project" in params else title, "attachments": [{"color": colour, "blocks": blocks}]}


def ready_message(project: str, channel: str, events: list[str]) -> dict:
    """The test message: what this channel will get, from which project. `events` are the
    entry's own words: `all`, a glob as written, or an event name."""
    colour, emoji = READY
    names = ", ".join("all events" if name == "all" else name if any(c in name for c in "*?[")
                      else event_words(name) for name in events) or "no events yet"
    return {"text": "Success, you're ready!", "attachments": [{"color": colour, "blocks": [
        {"type": "header", "text": {"type": "plain_text", "text": f"{emoji} Success, you're ready!"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": (
            f"pdt will post here for these events of the project *{project}*: {names}.")}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": (
            f"Test message to #{channel.lstrip('#')}, sent by `pdt alerts test`")}]},
    ]}]}


def webhook_url(name: str) -> str:
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
            value = (dotenv_values(path, encoding="utf-8-sig").get(name) or "").strip()
            if value:
                return value
    return ""


def post(url: str, payload: dict) -> None:
    """POST `payload` to a webhook. Raises when Slack does not answer `ok`."""
    request = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            body = response.read().decode(errors="replace").strip()
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace").strip()
        raise RuntimeError(f"Slack answered HTTP {e.code}: {body or e.reason}") from None
    except urllib.error.URLError as e:
        raise RuntimeError(f"Slack could not be reached: {e.reason}") from None
    if body != "ok":
        raise RuntimeError(f"Slack answered {body!r}, not ok")


def send(entry: dict, payload: dict) -> None:
    name = secret_name(str(entry["channel"]))
    url = webhook_url(name)
    if url == "":
        raise RuntimeError(f"{name} is not set; run pdt alerts setup to connect "
                           f"#{entry['channel']}")
    post(url, payload)


def deliver(event, entry: dict, unhandled: bool) -> None:
    send(entry, message(event, unhandled))
