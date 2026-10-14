"""`pdt alerts setup` and `pdt alerts test`.

`setup` asks which alert types to use (SETUPS), then for each one which events
it gets and the least it needs to work. It writes the entry into the top-level
`alerts:` of pdt.yml and the entry's secret into the project's `.env`, says what
it wrote, and sends the test message. `test` sends the same message to every
alerts entry of a type in SETUPS.

`deploy` calls `offer` before a deploy of an app with no `alerts:` key in
pdt.yml or its config.yml, when a person can answer. pdt offers it once per
computer: `alerts_offered` in .pdt/state records it, and `setup` sets it too.

A Slack entry connects in one of two ways. With the pdt Slack app (CLIENT_ID and
CONNECT_URL set), the browser opens Slack's own "Add to Slack" page, the user
picks a channel and clicks Allow, Slack sends its code to CONNECT_URL, which
sends the browser on to a server on 127.0.0.1 here with that code, and pdt
trades the code at CONNECT_URL for the webhook URL and its channel. CONNECT_URL
holds the app's client secret, so it is a server and not this program. Without
it, the user pastes a webhook URL, and pdt can open Slack with a filled-in app
manifest that makes one.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Callable

from dotenv import set_key

from pdt import config, console, lifecycle, slack
from pdt.utils.email_auth import can_prompt
from pdt.utils.env_secret import private_file

EVENTS = (lifecycle.APP_DEPLOY_STARTED, lifecycle.APP_DEPLOY_COMPLETED,
          lifecycle.APP_DEPLOY_FAILED, lifecycle.JOB_RUN_STARTED, lifecycle.JOB_RUN_COMPLETED,
          lifecycle.JOB_RUN_FAILED, lifecycle.JOB_PAUSED, lifecycle.JOB_UNPAUSED)
OFFERED = "alerts_offered"
# The pdt Slack app. Both stay empty until the app and its CONNECT_URL server exist;
# until then setup asks for a webhook URL.
CLIENT_ID = ""
CONNECT_URL = ""
CONNECT_SECONDS = 300
MANIFEST = {
    "display_information": {"name": "pdt alerts",
                            "description": "Posts the deploys and runs of pdt jobs.",
                            "background_color": "#1D9BD1"},
    "oauth_config": {"scopes": {"bot": ["incoming-webhook"]}},
    "settings": {"incoming_webhooks": {"incoming_webhooks_enabled": True}},
}


@dataclass(frozen=True)
class Setup:
    label: str
    events: tuple[str, ...]
    # The entry's own keys and its .env values, or None when the user gave up.
    configure: Callable[[], tuple[dict, dict[str, str]] | None]
    # Sends the test message of an entry; raises when it cannot.
    test: Callable[[dict, str], None]
    describe: Callable[[dict], str]


def checkboxes(question: str, options: list[tuple[str, str]], chosen) -> list[str]:
    """The values of `options` the user leaves on. Each number typed turns one on or off,
    and an empty answer accepts the list as shown."""
    on = set(chosen)
    while True:
        console.heading(question)
        for number, (value, label) in enumerate(options, 1):
            console.choice(number, f"[{'x' if value in on else ' '}] {label}")
        answer = console.ask("Type numbers to turn them on or off, or press Enter to go on")
        if answer == "":
            return [value for value, _ in options if value in on]
        numbers = answer.replace(",", " ").split()
        if not all(n.isdigit() and 1 <= int(n) <= len(options) for n in numbers):
            console.warn(f"Type numbers from 1 to {len(options)}, separated by spaces.")
            continue
        for n in numbers:
            on ^= {options[int(n) - 1][0]}


def slack_configure() -> tuple[dict, dict[str, str]] | None:
    found = connect_slack() if CLIENT_ID and CONNECT_URL else None
    if found is None:
        found = paste_webhook()
    if found is None:
        return None
    url, channel = found
    return {"channel": channel}, {slack.secret_name(channel): url}


def connect_slack() -> tuple[str, str] | None:
    """The webhook URL and channel from Slack's "Add to Slack" page, or None."""
    nonce = secrets.token_urlsafe(16)
    answer: dict[str, str] = {}
    answered = threading.Event()

    class Callback(BaseHTTPRequestHandler):
        def do_GET(self):
            query = {key: values[0] for key, values in
                     urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).items()}
            if query.get("state") != nonce:
                self.send_error(404)
                return
            answer.update(query)
            text = ("Slack is connected. You can close this tab." if "code" in query else
                    "Slack did not connect. Go back to the terminal.")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(text.encode())
            answered.set()

        def log_message(self, *_args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Callback)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    authorize = "https://slack.com/oauth/v2/authorize?" + urllib.parse.urlencode({
        "client_id": CLIENT_ID, "scope": "incoming-webhook", "redirect_uri": CONNECT_URL,
        "state": f"{server.server_port}.{nonce}"})
    console.say("pdt opens Slack in your browser. Pick the channel for alerts, then click "
                "Allow.")
    console.command(authorize, "open this if no browser opens")
    try:
        webbrowser.open(authorize)
        with console.waiting("Waiting for Slack"):
            answered.wait(CONNECT_SECONDS)
    finally:
        server.shutdown()
        server.server_close()
    if "code" not in answer:
        console.warn(f"Slack did not connect ({console.escape(answer.get('error', 'no answer'))}).")
        return None
    request = urllib.request.Request(
        CONNECT_URL, data=json.dumps({"code": answer["code"]}).encode(), method="POST",
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=slack.TIMEOUT_SECONDS) as response:
            webhook = json.loads(response.read())
    except (urllib.error.URLError, ValueError) as e:
        console.warn(f"pdt could not get the webhook from Slack: {console.escape(str(e))}")
        return None
    if not str(webhook.get("url", "")).startswith(("https://", "http://")):
        console.warn(f"Slack did not give a webhook: {console.escape(str(webhook.get('error')))}")
        return None
    return webhook["url"], str(webhook.get("channel") or "slack").lstrip("#")


def paste_webhook() -> tuple[str, str] | None:
    console.say("pdt posts to Slack through a webhook: a secret link that posts into one "
                "channel.")
    url = console.ask("Paste a webhook URL, or press Enter to make one in your browser")
    if url == "":
        create = ("https://api.slack.com/apps?new_app=1&manifest_json="
                  + urllib.parse.quote(json.dumps(MANIFEST)))
        webbrowser.open(create)
        console.command(create, "open this if no browser opens")
        console.bullet("1. Pick your workspace, click Next, then click Create.")
        console.bullet("2. Click Install to Workspace, pick the channel for alerts, and click "
                       "Allow.")
        console.bullet("3. Click Incoming Webhooks on the left, and copy the webhook URL at the "
                       "bottom.")
        url = console.ask("Paste the webhook URL")
    while True:
        if url == "":
            return None
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme in ("http", "https") and parsed.hostname:
            if parsed.hostname in slack.WEBHOOK_HOSTS or console.confirm(
                    f"{url} is not a Slack webhook address. Use it anyway?"):
                break
        else:
            console.warn("A Slack webhook URL starts with https://hooks.slack.com/services/. "
                         "Press Enter to stop.")
        url = console.ask("Paste the webhook URL")
    channel = ""
    while channel == "":
        channel = console.ask("Which channel does it post to? For example it-alerts").lstrip("#")
    return url, channel


def slack_test(entry: dict, project: str) -> None:
    slack.send(entry, slack.ready_message(project, entry["channel"], entry.get("events", [])))


SETUPS = {
    "slack": Setup("Slack: a message in a Slack channel",
                   (lifecycle.APP_DEPLOY_COMPLETED, lifecycle.APP_DEPLOY_FAILED,
                    lifecycle.JOB_RUN_FAILED),
                   slack_configure, slack_test, lambda entry: f"Slack #{entry['channel']}"),
}


def setup() -> int:
    project = config.find_project()
    if not can_prompt(None):
        console.error("pdt alerts setup asks questions; run it in a terminal")
        return 1
    state = config.read_state()
    state[OFFERED] = True
    config.write_state(state)
    types = checkboxes("Where should pdt send alerts?",
                       [(name, item.label) for name, item in SETUPS.items()], list(SETUPS)[:1])
    if not types:
        console.say("No alert type chosen, so nothing changed.")
        return 0
    code = 0
    for name in types:
        item = SETUPS[name]
        console.say()
        events = checkboxes(f"Which events should go to {item.label.partition(':')[0]}?",
                            [(event, f"{slack.event_words(event)} ({event})") for event in EVENTS],
                            item.events)
        if not events:
            console.say("No events chosen, so nothing changed for this type.")
            continue
        found = item.configure()
        if found is None:
            console.note(f"{console.value(name)} is not set up. Run "
                         f"{console.value('pdt alerts setup')} to try again.")
            code = 1
            continue
        keys, values = found
        entry = {"type": name, **keys, "events": events}
        alerts = config.load_yaml(project / config.PROJECT_FILE).get("alerts")
        alerts = [alert for alert in alerts if isinstance(alert, dict) and not (
            alert.get("type") == name and all(alert.get(k) == v for k, v in keys.items()))] \
            if isinstance(alerts, list) else []
        path = config.save_root_alerts([*alerts, entry])
        env_file = project / ".env"
        private_file(env_file)
        for key, value in values.items():
            set_key(env_file, key, value, quote_mode="never")
            os.environ[key] = value
        console.done(f"saved  the {console.value(item.describe(entry))} entry under "
                     f"{console.value('alerts:')} in {console.value(config.shown_path(path))}")
        if values:
            names = ", ".join(map(console.value, values))
            console.done(f"saved  {names} in "
                         f"{console.value(config.shown_path(env_file))}")
        for app in own_alerts_apps():
            console.note(f"{console.value(app)} sets its own {console.value('alerts:')}, so it "
                         "does not use this entry.")
        console.status(f"Sending a test message to {console.escape(item.describe(entry))}...")
        try:
            item.test(entry, project.name)
            console.done("sent.")
        except Exception as e:
            console.failed(f"not sent: {console.escape(str(e))}")
            code = 1
    console.say()
    console.next_steps([
        ("pdt alerts test", "send the test message again"),
        ("pdt deploy <app>", "send the new alerts settings to a deployed app"),
    ])
    return code


def own_alerts_apps() -> list[str]:
    """The apps whose pdt.yml apps: entry or config.yml sets alerts:, so the top-level one
    does not reach them."""
    root_cfg = config.load_yaml(config.find_project() / config.PROJECT_FILE)
    return [app for app in config.app_folders()
            if "alerts" in config.root_app_entry(root_cfg, app)
            or "alerts" in config.load_yaml(config.find_project() / app / config.APP_FILE)]


def entries() -> list[dict]:
    """Every alerts entry of the project and its apps of a type in SETUPS, each once."""
    found = lifecycle.alerts_for(None)
    for app in config.app_folders():
        try:
            found += config.merged_app(app)["alerts"]
        except config.ConfigError:
            continue
    unique = {repr(sorted(entry.items())): entry for entry in found if entry["type"] in SETUPS}
    return list(unique.values())


def test() -> int:
    project = config.find_project()
    found = entries()
    if not found:
        console.say("No alerts are set up yet.")
        console.command("pdt alerts setup", "choose where alerts go")
        return 1
    code = 0
    for entry in found:
        item = SETUPS[entry["type"]]
        try:
            item.test(entry, project.name)
            console.done(f"Sent the test message to {console.escape(item.describe(entry))}.")
        except Exception as e:
            console.failed(f"{console.escape(item.describe(entry))}: not sent: "
                           f"{console.escape(str(e))}")
            code = 1
    return code


def offer(app_name: str, assume_yes: bool) -> None:
    """Before a deploy, ask once per computer to set up alerts when the project has none."""
    if assume_yes or not can_prompt(None) or config.read_state().get(OFFERED):
        return
    root_cfg = config.load_yaml(config.find_project() / config.PROJECT_FILE)
    if "alerts" in root_cfg or app_name in own_alerts_apps():
        return
    console.say()
    console.say(f"pdt can post to Slack when {console.value(app_name)} deploys, or when a run "
                "fails.")
    try:
        answer = console.confirm("Set up alerts now?")
    except EOFError:
        answer = False
    if answer:
        setup()
        console.say()
        return
    state = config.read_state()
    state[OFFERED] = True
    config.write_state(state)
    console.say(f"Set them up later with {console.value('pdt alerts setup')}.")
