import json
import threading
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from conftest import add_app

from pdt import alerts_cli, config, deploy, lifecycle, slack

AT = datetime(2026, 10, 13, 14, 2, tzinfo=timezone.utc)
PARAMS = {"project": "contoso-jobs", "app": "sync-users", "provider": "aws"}


@pytest.fixture
def webhook():
    """A local stand-in for a Slack webhook: it records each JSON body and answers `ok`,
    or `answer` when a test sets it."""
    seen = []
    answer = {"status": 200, "body": "ok"}

    class Hook(BaseHTTPRequestHandler):
        def do_POST(self):
            seen.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(answer["status"])
            self.end_headers()
            self.wfile.write(answer["body"].encode())

        def log_message(self, *_args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Hook)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}/services/T1/B1/x", seen, answer
    server.shutdown()


def answers(monkeypatch, *replies):
    """Scripted answers for console.ask and console.confirm, in order."""
    queue = list(replies)
    monkeypatch.setattr(alerts_cli.console, "ask", lambda *_a, **_k: queue.pop(0))
    monkeypatch.setattr(alerts_cli.console, "confirm", lambda *_a, **_k: queue.pop(0) == "y")
    monkeypatch.setattr(alerts_cli, "can_prompt", lambda _interactive: True)
    monkeypatch.setattr(alerts_cli.webbrowser, "open", lambda _url: True)
    return queue


def test_a_failed_scheduled_run_is_red_and_lists_every_parameter_in_order():
    event = lifecycle.Event("job:run-failed", AT, {
        **PARAMS, "run_id": "20261013T140200Z", "deployed": True, "trigger": "schedule",
        "exit_code": 1, "state": "runs/*-20261013T140200Z/"})
    assert slack.message(event) == {
        "text": "Run failed: sync-users (contoso-jobs)",
        "attachments": [{"color": "#E01E5A", "blocks": [
            {"type": "header",
             "text": {"type": "plain_text", "text": ":red_circle: Run failed: sync-users"}},
            {"type": "section", "fields": [
                {"type": "mrkdwn", "text": "*Project*\ncontoso-jobs"},
                {"type": "mrkdwn", "text": "*App*\nsync-users"},
                {"type": "mrkdwn", "text": "*Provider*\naws"},
                {"type": "mrkdwn", "text": "*Where*\nDeployed job"},
                {"type": "mrkdwn", "text": "*Trigger*\nSchedule"},
                {"type": "mrkdwn", "text": "*Exit code*\n1"},
                {"type": "mrkdwn", "text": "*Run id*\n20261013T140200Z"},
                {"type": "mrkdwn", "text": "*State*\nruns/*-20261013T140200Z/"}]},
            {"type": "context", "elements": [{"type": "mrkdwn", "text":
                "`job:run-failed` at 2026-10-13 14:02 UTC, sent by pdt"}]}]}]}


@pytest.mark.parametrize(("name", "colour", "emoji"), [
    ("app:deploy-completed", "#2EB67D", ":large_green_circle:"),
    ("app:deploy-failed", "#E01E5A", ":red_circle:"),
    ("job:paused", "#1D9BD1", ":large_blue_circle:"),
])
def test_the_colour_and_the_title_say_the_outcome(name, colour, emoji):
    attachment = slack.message(lifecycle.Event(name, AT, PARAMS))["attachments"][0]
    assert attachment["color"] == colour
    assert attachment["blocks"][0]["text"]["text"].startswith(f"{emoji} ")


def test_an_unhandled_event_is_grey_and_says_why_it_came():
    blocks = slack.message(lifecycle.Event("job:run-started", AT, PARAMS), unhandled=True)
    assert blocks["attachments"][0]["color"] == "#868686"
    assert "*Unhandled event.*" in blocks["attachments"][0]["blocks"][1]["elements"][0]["text"]


def test_a_url_parameter_becomes_a_button():
    event = lifecycle.Event("job:run-failed", AT, {**PARAMS, "url": "https://example.com/run/1"})
    blocks = slack.message(event)["attachments"][0]["blocks"]
    assert {"type": "actions", "elements": [{"type": "button", "url": "https://example.com/run/1",
                                             "text": {"type": "plain_text", "text": "Open"}}]} in blocks


def test_the_ready_message_names_the_project_and_the_events():
    payload = slack.ready_message("contoso-jobs", "it-alerts", ["job:run-failed"])
    assert payload["text"] == "Success, you're ready!"
    assert "*contoso-jobs*" in payload["attachments"][0]["blocks"][1]["text"]["text"]
    assert "Run failed" in payload["attachments"][0]["blocks"][1]["text"]["text"]


def test_deliver_posts_the_message_to_the_webhook_of_the_channel(webhook, monkeypatch):
    url, seen, _answer = webhook
    monkeypatch.setenv("SLACK_WEBHOOK_IT_ALERTS", url)
    event = lifecycle.Event("app:deploy-failed", AT, PARAMS)
    slack.deliver(event, {"type": "slack", "channel": "#it-alerts"}, False)
    assert seen == [slack.message(event)]


def test_a_slack_error_raises_with_slacks_answer(webhook, monkeypatch):
    url, _seen, answer = webhook
    answer.update(status=404, body="no_service")
    monkeypatch.setenv("SLACK_WEBHOOK_IT_ALERTS", url)
    with pytest.raises(RuntimeError, match="Slack answered HTTP 404: no_service"):
        slack.deliver(lifecycle.Event("job:paused", AT, PARAMS), {"channel": "it-alerts"}, False)


def test_a_missing_webhook_names_the_env_var_and_the_fix(project, monkeypatch):
    monkeypatch.delenv("SLACK_WEBHOOK_IT_ALERTS", raising=False)
    monkeypatch.delenv("PDT_ENV_JSON", raising=False)
    with pytest.raises(RuntimeError, match="SLACK_WEBHOOK_IT_ALERTS is not set; run pdt alerts setup"):
        slack.deliver(lifecycle.Event("job:paused", AT, PARAMS), {"channel": "it-alerts"}, False)


def test_a_cloud_job_finds_the_webhook_in_its_env_secret(monkeypatch):
    monkeypatch.delenv("SLACK_WEBHOOK_OPS", raising=False)
    monkeypatch.setenv("PDT_ENV_JSON", json.dumps({"SLACK_WEBHOOK_OPS": "https://hooks.slack.com/x"}))
    assert slack.webhook_url("SLACK_WEBHOOK_OPS") == "https://hooks.slack.com/x"


def test_a_slack_entry_needs_a_channel_and_takes_no_other_key(project):
    with pytest.raises(config.ConfigError, match="the slack alerts entry needs channel"):
        lifecycle.parse_alerts([{"type": "slack", "events": ["all"]}], "pdt.yml")
    with pytest.raises(config.ConfigError, match="a slack entry takes: type, events, "
                                                 "exclude-events, channel"):
        lifecycle.parse_alerts([{"type": "slack", "channel": "a", "url": "x", "events": ["all"]}],
                               "pdt.yml")


def test_the_webhook_of_each_slack_entry_is_a_required_env_var_so_deploy_sends_it(project):
    (project / "pdt.yml").write_text("platform:\n  provider: azure\nalerts:\n"
                                     "  - type: slack\n    channel: it-alerts\n    events: [all]\n")
    add_app(project, "report", "env:\n  required: [API_KEY]\n")
    assert config.env_spec(config.merged_app("report"))["required"] == [
        "API_KEY", "SLACK_WEBHOOK_IT_ALERTS"]


def test_save_root_alerts_replaces_only_the_alerts_block(project):
    (project / "pdt.yml").write_text(
        "# my project\nplatform:\n  provider: azure  # keep\nalerts:\n  - type: console\n"
        "    events: [all]\n\napps:\n  - name: report\n")
    config.save_root_alerts([{"type": "slack", "channel": "it-alerts", "events": ["job:*"]}])
    assert (project / "pdt.yml").read_text() == (
        "# my project\nplatform:\n  provider: azure  # keep\nalerts:\n  - type: slack\n"
        "    channel: it-alerts\n    events: [\"job:*\"]\n\napps:\n  - name: report\n")


def test_setup_with_a_pasted_webhook_writes_pdt_yml_and_env_then_sends_the_test(
        project, webhook, monkeypatch):
    url, seen, _answer = webhook
    monkeypatch.delenv("SLACK_WEBHOOK_IT_ALERTS", raising=False)
    answers(monkeypatch, "", "1", "", url, "y", "#it-alerts")
    assert alerts_cli.setup() == 0
    assert config.load_yaml(project / "pdt.yml")["alerts"] == [{
        "type": "slack", "channel": "it-alerts",
        "events": ["app:deploy-started", "app:deploy-completed", "app:deploy-failed",
                   "job:run-failed"]}]
    assert (project / ".env").read_text() == f"SLACK_WEBHOOK_IT_ALERTS={url}\n"
    assert [payload["text"] for payload in seen] == ["Success, you're ready!"]
    assert config.read_state()["alerts_offered"] is True


def test_setup_again_for_the_same_channel_replaces_its_entry(project, webhook, monkeypatch):
    url, _seen, _answer = webhook
    monkeypatch.delenv("SLACK_WEBHOOK_IT_ALERTS", raising=False)
    answers(monkeypatch, "", "", url, "y", "it-alerts")
    alerts_cli.setup()
    answers(monkeypatch, "", "4", "", url, "y", "it-alerts")
    alerts_cli.setup()
    assert [entry["events"] for entry in config.load_yaml(project / "pdt.yml")["alerts"]] == [
        ["app:deploy-completed", "app:deploy-failed", "job:run-started", "job:run-failed"]]


def test_setup_through_the_slack_app_gets_the_webhook_and_channel_from_connect_url(
        project, webhook, monkeypatch):
    url, seen, _answer = webhook
    monkeypatch.delenv("SLACK_WEBHOOK_OPS", raising=False)
    codes = []

    class Connect(BaseHTTPRequestHandler):
        def do_POST(self):
            codes.append(json.loads(self.rfile.read(int(self.headers["Content-Length"])))["code"])
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps({"url": url, "channel": "#ops"}).encode())

        def log_message(self, *_args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Connect)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(alerts_cli, "CLIENT_ID", "123.456")
    monkeypatch.setattr(alerts_cli, "CONNECT_URL", f"http://127.0.0.1:{server.server_port}/slack")
    answers(monkeypatch, "", "")
    opened = []

    def browser(authorize):
        # Slack, then CONNECT_URL, send the browser on to pdt's server with the code.
        query = urllib.parse.parse_qs(urllib.parse.urlparse(authorize).query)
        opened.append(query)
        port, _, _nonce = query["state"][0].partition(".")
        callback = f"http://127.0.0.1:{port}/?" + urllib.parse.urlencode(
            {"code": "slack-code", "state": query["state"][0].partition(".")[2]})
        threading.Thread(target=urllib.request.urlopen, args=(callback,), daemon=True).start()
        return True

    monkeypatch.setattr(alerts_cli.webbrowser, "open", browser)
    try:
        assert alerts_cli.setup() == 0
    finally:
        server.shutdown()
    assert opened[0]["scope"] == ["incoming-webhook"]
    assert opened[0]["client_id"] == ["123.456"]
    assert codes == ["slack-code"]
    assert config.load_yaml(project / "pdt.yml")["alerts"][0]["channel"] == "ops"
    assert [payload["text"] for payload in seen] == ["Success, you're ready!"]


def test_alerts_test_sends_the_ready_message_to_every_slack_entry(project, webhook, monkeypatch):
    url, seen, _answer = webhook
    (project / "pdt.yml").write_text("platform:\n  provider: azure\nalerts:\n"
                                     "  - type: slack\n    channel: ops\n    events: [all]\n")
    add_app(project, "report", "alerts:\n  - type: slack\n    channel: report-alerts\n"
                               "    events: [job:run-failed]\n")
    monkeypatch.setenv("SLACK_WEBHOOK_OPS", url)
    monkeypatch.setenv("SLACK_WEBHOOK_REPORT_ALERTS", url)
    assert alerts_cli.test() == 0
    assert [payload["text"] for payload in seen] == ["Success, you're ready!"] * 2


def offer_setup(monkeypatch):
    calls = []
    monkeypatch.setattr(alerts_cli, "setup", lambda: calls.append("setup") or 0)
    return calls


def test_the_first_deploy_offers_setup_once_and_records_it(project, monkeypatch):
    add_app(project, "report")
    calls = offer_setup(monkeypatch)
    answers(monkeypatch, "n")
    alerts_cli.offer("report", assume_yes=False)
    alerts_cli.offer("report", assume_yes=False)
    assert calls == []
    assert config.read_state()["alerts_offered"] is True


def test_yes_at_the_offer_runs_setup(project, monkeypatch):
    add_app(project, "report")
    calls = offer_setup(monkeypatch)
    answers(monkeypatch, "y")
    alerts_cli.offer("report", assume_yes=False)
    assert calls == ["setup"]


@pytest.mark.parametrize("why", ["--yes", "no terminal", "alerts in pdt.yml", "alerts in app"])
def test_no_offer_with_yes_without_a_terminal_or_with_an_alerts_key(project, monkeypatch, why):
    add_app(project, "report", "alerts: []\n" if why == "alerts in app" else "")
    if why == "alerts in pdt.yml":
        (project / "pdt.yml").write_text("platform:\n  provider: azure\nalerts: []\n")
    calls = offer_setup(monkeypatch)
    answers(monkeypatch, "y")
    if why == "no terminal":
        monkeypatch.setattr(alerts_cli, "can_prompt", lambda _interactive: False)
    alerts_cli.offer("report", assume_yes=why == "--yes")
    assert calls == []
    assert "alerts_offered" not in config.read_state()


def test_deploy_offers_alerts_before_it_dispatches(project, monkeypatch):
    add_app(project, "report", "schedule: daily\n")
    order = []
    monkeypatch.setattr(deploy.regions, "choose_region", lambda *_a: "")
    monkeypatch.setattr(deploy.alerts_cli, "offer", lambda app, yes: order.append(("offer", app)))
    monkeypatch.setattr(deploy, "dispatch", lambda *a, **_k: order.append(("dispatch", a[1])) or 1)
    deploy.deploy("report", assume_yes=False, run=False)
    assert order == [("offer", "report"), ("dispatch", "deploy")]
