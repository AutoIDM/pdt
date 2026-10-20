"""The slack alerts type: a Block Kit message in one Slack channel, through an incoming webhook.

An entry is `{type: slack, channel: <name>}` plus its events. The webhook URL is
a secret, so it lives in `.env` as SLACK_WEBHOOK_<CHANNEL> (`secret_name`), and
`config.env_spec` adds that name to every app's env spec, so `pdt deploy` sends
it in the app's cloud secret. A Slack webhook posts to the one channel it was
made for; `channel` names that channel and the env var, and a new channel
needs a new webhook from `pdt alerts setup`.

The message name and icon come from the Slack app that made the webhook; a
webhook cannot set its own name or icon.

This module is in deploy_common.LIFECYCLE_MODULES, so it imports only what
lifecycle.py's script header installs.
"""

from __future__ import annotations

from pdt import alert_message
from pdt.alert_message import Message

KEYS = frozenset({"channel"})
# Slack, and Slack for government (GovSlack).
WEBHOOK_HOSTS = ("hooks.slack.com", "hooks.slack-gov.com")


def secret_name(channel: str) -> str:
    """The env var that holds the webhook of `channel`: #it-alerts is SLACK_WEBHOOK_IT_ALERTS."""
    return alert_message.env_name("SLACK_WEBHOOK_", channel)


def secrets(entry: dict) -> list[str]:
    return [secret_name(str(entry["channel"]))]


def blocks(message: Message) -> dict:
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


def ready_message(project: str, channel: str, events: list[str]) -> dict:
    return blocks(alert_message.ready_message(project, f"#{channel.lstrip('#')}", events))


def send(entry: dict, payload: dict) -> None:
    name = secret_name(str(entry["channel"]))
    url = alert_message.secret_value(name)
    if url == "":
        raise RuntimeError(f"{name} is not set; run pdt alerts setup to connect "
                           f"#{entry['channel']}")
    body = alert_message.post(url, payload, "Slack")
    if body != "ok":
        raise RuntimeError(f"Slack answered {body!r}, not ok")


def deliver(event, entry: dict, unhandled: bool) -> None:
    send(entry, message(event, unhandled))
