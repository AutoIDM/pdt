"""The teams alerts type: an Adaptive Card in one Microsoft Teams channel, through a Teams workflow.

An entry is `{type: teams, channel: <name>}` plus its events. The workflow URL is
a secret, so it lives in `.env` as TEAMS_WEBHOOK_<CHANNEL> (`secret_name`), and
`config.env_spec` adds that name to every app's env spec, so `pdt deploy` sends
it in the app's cloud secret. A Teams workflow posts to the one channel that was
picked when it was made; `channel` names that channel and the env var. The card
goes through the Workflows trigger "When a Teams webhook request is received",
which answers 202 Accepted, so any 2xx answer is success.

Teams cards have no inline code, so `teams_text` turns `*bold*` into bold and
drops the backticks.
"""

from __future__ import annotations

import re

from pdt import alert_message
from pdt.alert_message import Message

KEYS = frozenset({"channel"})
# A Workflows URL host is <id>.environment.api.powerplatform.com.
WEBHOOK_HOST = ".api.powerplatform.com"
CARD_VERSION = "1.2"


def secret_name(channel: str) -> str:
    """The env var that holds the workflow URL of `channel`: #it-alerts is TEAMS_WEBHOOK_IT_ALERTS."""
    return alert_message.env_name("TEAMS_WEBHOOK_", channel)


def secrets(entry: dict) -> list[str]:
    return [secret_name(str(entry["channel"]))]


def teams_text(text: str) -> str:
    return re.sub(r"\*([^*]+)\*", r"**\1**", text).replace("`", "")


def card(message: Message) -> dict:
    tone = message.tone
    body: list[dict] = [{"type": "Container", "style": tone.style, "bleed": True, "items": [{
        "type": "TextBlock", "text": f"{tone.emoji} {message.title}", "size": "Large",
        "weight": "Bolder", "wrap": True}]}]
    if message.note:
        body.append({"type": "TextBlock", "text": teams_text(message.note), "isSubtle": True,
                     "size": "Small", "wrap": True})
    if message.text:
        body.append({"type": "TextBlock", "text": teams_text(message.text), "wrap": True})
    if message.fields:
        body.append({"type": "FactSet", "facts": [
            {"title": label, "value": teams_text(value)} for label, value in message.fields]})
    if message.footer:
        body.append({"type": "TextBlock", "text": teams_text(message.footer), "isSubtle": True,
                     "size": "Small", "wrap": True})
    content: dict = {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json", "type": "AdaptiveCard",
        "version": CARD_VERSION, "fallbackText": message.summary, "body": body}
    if message.url:
        content["actions"] = [{"type": "Action.OpenUrl", "title": "Open", "url": message.url}]
    return {"type": "message", "attachments": [{
        "contentType": "application/vnd.microsoft.card.adaptive", "contentUrl": None,
        "content": content}]}


def message(event, unhandled: bool = False) -> dict:
    return card(alert_message.event_message(event, unhandled))


def ready_message(project: str, channel: str, events: list[str]) -> dict:
    return card(alert_message.ready_message(project, channel, events))


def send(entry: dict, payload: dict) -> None:
    name = secret_name(str(entry["channel"]))
    url = alert_message.secret_value(name)
    if url == "":
        raise RuntimeError(f"{name} is not set; run pdt alerts setup to connect Teams "
                           f"{entry['channel']}")
    alert_message.post(url, payload, "Teams")


def deliver(event, entry: dict, unhandled: bool) -> None:
    send(entry, message(event, unhandled))
