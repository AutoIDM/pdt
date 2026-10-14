"""The slack alerts type: a Block Kit message in one Slack channel, through an incoming webhook.

An entry is `{type: slack, webhook: <id>}` plus its events, and an optional
`name`. A Slack webhook URL holds the workspace and webhook IDs, not the
channel. `webhook_id` takes the webhook ID (B0123ABC) from the URL, or a hash
of the URL when it has none. The URL is a secret, so it lives in `.env` as
SLACK_WEBHOOK_<ID> (`secret_name`), and `config.env_spec` adds that name to
every app's env spec, so `pdt deploy` sends it in the app's cloud secret.
`name` is a label that pdt shows in its own text (`label`), such as #it-alerts.
An entry of pdt 0.1.9, `{type: slack, channel: <name>}`, still works: its
`channel` names the env var, SLACK_WEBHOOK_IT_ALERTS for it-alerts. An entry
sets exactly one of `webhook` and `channel`.

The content of a message comes from pdt.alert_message, which also finds the URL
(`secret_value`) and posts it; `blocks` lays a Message out as Block Kit. The
message name and icon come from the Slack app that made the webhook, and the
colour bar from the Message's tone; a webhook cannot set its own name or icon.

This module is in deploy_common.LIFECYCLE_MODULES, so it imports only what
lifecycle.py's script header installs.
"""

from __future__ import annotations

import re
import urllib.parse

from pdt import alert_message
from pdt.alert_message import Message

KEYS = frozenset({"webhook", "name", "channel"})
ONE_OF = ("webhook", "channel")
# Slack, and Slack for government (GovSlack).
WEBHOOK_HOSTS = ("hooks.slack.com", "hooks.slack-gov.com")


def webhook_id(url: str) -> str:
    """The B0123ABC of https://hooks.slack.com/services/T0123ABC/B0123ABC/<secret>."""
    found = re.search(r"/services/[^/]+/([A-Za-z0-9]+)/", urllib.parse.urlparse(url).path)
    return found.group(1) if found else alert_message.url_id(url)


def label(entry: dict) -> str:
    """The entry as pdt names it: its `name`, its old #channel, or its webhook ID."""
    if entry.get("name"):
        return str(entry["name"])
    if entry.get("channel"):
        return f"#{str(entry['channel']).lstrip('#')}"
    return f"webhook {entry['webhook']}"


def secret_name(entry: dict) -> str:
    """The env var that holds the entry's webhook: SLACK_WEBHOOK_B0123ABC, or
    SLACK_WEBHOOK_IT_ALERTS for an old `channel: it-alerts`."""
    return alert_message.env_name("SLACK_WEBHOOK_", str(entry.get("webhook") or entry["channel"]))


def secrets(entry: dict) -> list[str]:
    return [secret_name(entry)]


def blocks(message: Message) -> dict:
    """The Block Kit payload of a Message."""
    tone = message.tone
    layout: list[dict] = [{"type": "header", "text": {
        "type": "plain_text", "text": f"{tone.slack_emoji} {message.title}"}}]
    if message.note:
        layout.append({"type": "context", "elements": [{"type": "mrkdwn", "text": message.note}]})
    if message.text:
        layout.append({"type": "section", "text": {"type": "mrkdwn", "text": message.text}})
    if message.fields:
        layout.append({"type": "section", "fields": [
            {"type": "mrkdwn", "text": f"*{label}*\n{value}"} for label, value in message.fields]})
    if message.url:
        layout.append({"type": "actions", "elements": [{
            "type": "button", "text": {"type": "plain_text", "text": "Open"},
            "url": message.url}]})
    if message.footer:
        layout.append({"type": "context", "elements": [{"type": "mrkdwn", "text": message.footer}]})
    return {"text": message.summary, "attachments": [{"color": tone.colour, "blocks": layout}]}


def message(event, unhandled: bool = False) -> dict:
    return blocks(alert_message.event_message(event, unhandled))


def ready_message(project: str, entry: dict) -> dict:
    return blocks(alert_message.ready_message(project, label(entry), entry.get("events", [])))


def send(entry: dict, payload: dict) -> None:
    """POST `payload` to the entry's webhook. Raises when Slack does not answer `ok`."""
    name = secret_name(entry)
    url = alert_message.secret_value(name)
    if url == "":
        raise RuntimeError(f"{name} is not set; run pdt alerts setup to connect Slack "
                           f"{label(entry)}")
    body = alert_message.post(url, payload, "Slack")
    if body != "ok":
        raise RuntimeError(f"Slack answered {body!r}, not ok")


def deliver(event, entry: dict, unhandled: bool) -> None:
    send(entry, message(event, unhandled))
