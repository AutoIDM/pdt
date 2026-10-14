"""The teams alerts type: an Adaptive Card in one Microsoft Teams channel, through a Teams workflow.

An entry is `{type: teams, webhook: <id>}` plus its events, and an optional
`name`. `webhook_id` takes the workflow ID from the URL (the part after
/workflows/), or a hash of the URL when it has none. The URL is a secret, so it
lives in `.env` as TEAMS_WEBHOOK_<ID> (`secret_name`), and `config.env_spec`
adds that name to every app's env spec, so `pdt deploy` sends it in the app's
cloud secret. A Teams workflow posts to the one channel that was picked when it
was made. `name` is a label that pdt shows in its own text (`label`), such as
the name of that channel. The card goes through the Workflows trigger "When a
Teams webhook request is received", which answers 202 Accepted, so any 2xx
answer is success.

Teams cards have no inline code, so `teams_text` turns `*bold*` into bold and
drops the backticks.
"""

from __future__ import annotations

import re
import urllib.parse

from pdt import alert_message
from pdt.alert_message import Message

KEYS = frozenset({"webhook", "name"})
ONE_OF = ("webhook",)
# A Workflows URL host is <id>.environment.api.powerplatform.com.
WEBHOOK_HOST = ".api.powerplatform.com"
CARD_VERSION = "1.2"


def webhook_id(url: str) -> str:
    """The workflow ID of a Workflows URL: the part after /workflows/."""
    found = re.search(r"/workflows/([A-Za-z0-9]+)(?:/|$)", urllib.parse.urlparse(url).path)
    return found.group(1) if found else alert_message.url_id(url)


def label(entry: dict) -> str:
    """The entry as pdt names it: its `name`, or its workflow ID."""
    if entry.get("name"):
        return str(entry["name"])
    return f"workflow {entry['webhook']}"


def secret_name(entry: dict) -> str:
    """The env var that holds the entry's workflow URL: TEAMS_WEBHOOK_<ID>."""
    return alert_message.env_name("TEAMS_WEBHOOK_", str(entry["webhook"]))


def secrets(entry: dict) -> list[str]:
    return [secret_name(entry)]


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


def ready_message(project: str, entry: dict) -> dict:
    return card(alert_message.ready_message(project, label(entry), entry.get("events", [])))


def send(entry: dict, payload: dict) -> None:
    name = secret_name(entry)
    url = alert_message.secret_value(name)
    if url == "":
        raise RuntimeError(f"{name} is not set; run pdt alerts setup to connect Teams "
                           f"{label(entry)}")
    alert_message.post(url, payload, "Teams")


def deliver(event, entry: dict, unhandled: bool) -> None:
    send(entry, message(event, unhandled))
