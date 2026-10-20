import json
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
from conftest import add_app

from pdt import alert_message, alerts_cli, config, lifecycle, teams

AT = datetime(2026, 10, 13, 14, 2, tzinfo=timezone.utc)
PARAMS = {"project": "contoso-jobs", "app": "sync-users", "provider": "aws"}


@pytest.fixture
def workflow():
    seen = []
    answer = {"status": 202, "body": ""}

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
    yield f"http://127.0.0.1:{server.server_port}/workflows/w1", seen, answer
    server.shutdown()


def answers(monkeypatch, *replies):
    queue = list(replies)
    monkeypatch.setattr(alerts_cli.console, "ask", lambda *_a, **_k: queue.pop(0))
    monkeypatch.setattr(alerts_cli.console, "confirm", lambda *_a, **_k: queue.pop(0) == "y")
    monkeypatch.setattr(alerts_cli, "can_prompt", lambda _interactive: True)
    monkeypatch.setattr(alerts_cli.webbrowser, "open", lambda _url: True)
    return queue


def body(payload):
    return payload["attachments"][0]["content"]["body"]


def test_a_failed_scheduled_run_is_a_red_card_that_lists_every_parameter_in_order():
    event = lifecycle.Event("job:run-failed", AT, {
        **PARAMS, "run_id": "20261013T140200Z", "deployed": True, "trigger": "schedule",
        "exit_code": 1, "state": "runs/*-20261013T140200Z/"})
    assert teams.message(event) == {"type": "message", "attachments": [{
        "contentType": "application/vnd.microsoft.card.adaptive", "contentUrl": None,
        "content": {
            "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
            "type": "AdaptiveCard", "version": "1.2",
            "fallbackText": "Run failed: sync-users (contoso-jobs)",
            "body": [
                {"type": "Container", "style": "attention", "bleed": True, "items": [
                    {"type": "TextBlock", "text": "\U0001F534 Run failed: sync-users",
                     "size": "Large", "weight": "Bolder", "wrap": True}]},
                {"type": "FactSet", "facts": [
                    {"title": "Project", "value": "contoso-jobs"},
                    {"title": "App", "value": "sync-users"},
                    {"title": "Provider", "value": "aws"},
                    {"title": "Where", "value": "Cloud run on AWS"},
                    {"title": "Trigger", "value": "Schedule"},
                    {"title": "Exit code", "value": "1"},
                    {"title": "Run id", "value": "20261013T140200Z"},
                    {"title": "Logs", "value": "pdt logs sync-users --id 20261013T140200Z"}]},
                {"type": "TextBlock", "text": "job:run-failed at 2026-10-13 14:02 UTC, sent by pdt",
                 "isSubtle": True, "size": "Small", "wrap": True}]}}]}


@pytest.mark.parametrize(("name", "style", "emoji"), [
    ("app:deploy-completed", "good", "\U0001F7E2"),
    ("app:deploy-failed", "attention", "\U0001F534"),
    ("job:paused", "accent", "\U0001F535"),
])
def test_the_style_and_the_title_say_the_outcome(name, style, emoji):
    card = body(teams.message(lifecycle.Event(name, AT, PARAMS)))[0]
    assert card["style"] == style
    assert card["items"][0]["text"].startswith(f"{emoji} ")


def test_an_unhandled_event_is_grey_and_says_why_it_came():
    card = body(teams.message(lifecycle.Event("job:run-started", AT, PARAMS), unhandled=True))
    assert card[0]["style"] == "emphasis"
    assert card[0]["items"][0]["text"].startswith("⚪ ")
    assert card[1]["text"] == ("**Unhandled event.** No alerts entry lists job:run-started, so it "
                               "came here through unhandled.")


def test_a_url_parameter_becomes_an_open_link():
    event = lifecycle.Event("job:run-failed", AT, {**PARAMS, "url": "https://example.com/run/1"})
    content = teams.message(event)["attachments"][0]["content"]
    assert content["actions"] == [{"type": "Action.OpenUrl", "title": "Open",
                                   "url": "https://example.com/run/1"}]


def test_the_ready_card_names_the_project_and_the_events():
    content = teams.ready_message("contoso-jobs", "it-alerts", ["job:run-failed"])[
        "attachments"][0]["content"]
    assert content["fallbackText"] == "Success, you're ready!"
    assert content["body"][1]["text"] == (
        "pdt will post here for these events of the project **contoso-jobs**: Run failed.")
    assert content["body"][2]["text"] == "Test message to it-alerts, sent by pdt alerts test"


def test_deliver_posts_the_card_to_the_workflow_of_the_channel(workflow, monkeypatch):
    url, seen, _answer = workflow
    monkeypatch.setenv("TEAMS_WEBHOOK_IT_ALERTS", url)
    event = lifecycle.Event("app:deploy-failed", AT, PARAMS)
    teams.deliver(event, {"type": "teams", "channel": "it-alerts"}, False)
    assert seen == [teams.message(event)]


def test_a_teams_error_raises_with_teams_answer(workflow, monkeypatch):
    url, _seen, answer = workflow
    answer.update(status=400, body="bad card")
    monkeypatch.setenv("TEAMS_WEBHOOK_IT_ALERTS", url)
    with pytest.raises(RuntimeError, match="Teams answered HTTP 400: bad card"):
        teams.deliver(lifecycle.Event("job:paused", AT, PARAMS), {"channel": "it-alerts"}, False)


def test_a_missing_workflow_url_names_the_env_var_and_the_fix(project, monkeypatch):
    monkeypatch.delenv("TEAMS_WEBHOOK_IT_ALERTS", raising=False)
    monkeypatch.delenv("PDT_ENV_JSON", raising=False)
    with pytest.raises(RuntimeError, match="TEAMS_WEBHOOK_IT_ALERTS is not set; run pdt alerts "
                                           "setup to connect Teams it-alerts"):
        teams.deliver(lifecycle.Event("job:paused", AT, PARAMS), {"channel": "it-alerts"}, False)


def test_a_cloud_job_finds_the_workflow_url_in_its_env_secret(monkeypatch):
    monkeypatch.delenv("TEAMS_WEBHOOK_OPS", raising=False)
    monkeypatch.setenv("PDT_ENV_JSON", json.dumps({"TEAMS_WEBHOOK_OPS": "https://x.api.powerplatform.com/w"}))
    assert alert_message.secret_value(teams.secret_name("ops")) == "https://x.api.powerplatform.com/w"


def test_a_teams_entry_needs_a_channel_and_takes_no_other_key(project):
    with pytest.raises(config.ConfigError, match="the teams alerts entry needs channel"):
        lifecycle.parse_alerts([{"type": "teams", "events": ["all"]}], "pdt.yml")
    with pytest.raises(config.ConfigError, match="a teams entry takes: type, events, "
                                                 "exclude-events, channel"):
        lifecycle.parse_alerts([{"type": "teams", "channel": "a", "url": "x", "events": ["all"]}],
                               "pdt.yml")


def test_the_workflow_url_of_each_teams_entry_is_a_required_env_var_so_deploy_sends_it(project):
    (project / "pdt.yml").write_text("platform:\n  provider: azure\nalerts:\n"
                                     "  - type: teams\n    channel: it-alerts\n    events: [all]\n")
    add_app(project, "report", "env:\n  required: [API_KEY]\n")
    assert config.env_spec(config.merged_app("report"))["required"] == [
        "API_KEY", "TEAMS_WEBHOOK_IT_ALERTS"]


def test_teams_starts_unchecked_and_setup_writes_pdt_yml_and_env_then_sends_the_test_on_yes(
        project, workflow, monkeypatch):
    url, seen, _answer = workflow
    monkeypatch.delenv("TEAMS_WEBHOOK_IT_ALERTS", raising=False)
    shown = {}
    questions = []

    def choice(_number, label):
        if questions.count(questions[-1]) == 1 and label.startswith("[x]"):
            shown.setdefault(questions[-1], []).append(label)

    monkeypatch.setattr(alerts_cli.console, "heading", questions.append)
    monkeypatch.setattr(alerts_cli.console, "choice", choice)
    answers(monkeypatch, "1 2", "", "", url, "y", "#it-alerts", "y")
    assert alerts_cli.setup() == 0
    assert shown["Where should pdt send alerts?"] == ["[x] Slack: a message in a Slack channel"]
    assert config.load_yaml(project / "pdt.yml")["alerts"] == [{
        "type": "teams", "channel": "it-alerts",
        "events": ["app:deploy-completed", "app:deploy-failed", "job:run-failed"]}]
    assert (project / ".env").read_text() == f"TEAMS_WEBHOOK_IT_ALERTS={url}\n"
    assert [payload["attachments"][0]["content"]["fallbackText"] for payload in seen] == [
        "Success, you're ready!"]


def test_a_host_outside_power_platform_asks_to_use_it_anyway(project, monkeypatch):
    asked = []
    answers(monkeypatch, "1 2", "", "", "https://example.com/hook", "it-alerts")
    monkeypatch.setattr(alerts_cli.console, "confirm", lambda text, **_k: asked.append(text) or "anyway" in text)
    assert alerts_cli.setup() == 0
    assert asked[0] == "https://example.com/hook is not a Teams workflow address. Use it anyway?"
    assert (project / ".env").read_text() == "TEAMS_WEBHOOK_IT_ALERTS=https://example.com/hook\n"


def test_enter_at_the_url_prints_the_workflows_steps(project, monkeypatch):
    lines = []
    monkeypatch.setattr(alerts_cli.console, "bullet", lines.append)
    answers(monkeypatch, "1 2", "", "", "", "https://a.environment.api.powerplatform.com/w", "ops", "n")
    assert alerts_cli.setup() == 0
    assert len(lines) == 4
    assert lines[0].startswith("1. In Teams, go to the channel for alerts")
    assert lines[1] == "2. Pick the template Send webhook alerts to a channel."


def test_a_url_that_is_not_a_web_address_warns_and_asks_again(project, monkeypatch):
    warned = []
    monkeypatch.setattr(alerts_cli.console, "warn", warned.append)
    answers(monkeypatch, "1 2", "", "", "teams", "https://a.environment.api.powerplatform.com/w", "ops",
            "n")
    assert alerts_cli.setup() == 0
    assert warned == ["A Teams workflow URL starts with https:// and ends in "
                      ".api.powerplatform.com. Press Enter to stop."]


def test_the_default_answer_to_the_test_question_sends_nothing(project, workflow, monkeypatch):
    url, seen, _answer = workflow
    answers(monkeypatch, "1 2", "", "", url, "y", "it-alerts", "n")
    assert alerts_cli.setup() == 0
    assert seen == []


def test_alerts_test_sends_the_ready_card_to_a_teams_entry_and_a_slack_entry(
        project, monkeypatch):
    sent = []
    monkeypatch.setattr(alert_message, "post", lambda _url, payload, service: sent.append(
        (service, payload)) or ("ok" if service == "Slack" else ""))
    (project / "pdt.yml").write_text("platform:\n  provider: azure\nalerts:\n"
                                     "  - type: slack\n    channel: ops\n    events: [all]\n"
                                     "  - type: teams\n    channel: ops\n    events: [all]\n")
    monkeypatch.setenv("SLACK_WEBHOOK_OPS", "https://hooks.slack.com/x")
    monkeypatch.setenv("TEAMS_WEBHOOK_OPS", "https://a.environment.api.powerplatform.com/w")
    assert alerts_cli.test() == 0
    assert [service for service, _payload in sent] == ["Slack", "Teams"]
    assert sent[0][1]["text"] == sent[1][1]["attachments"][0]["content"]["fallbackText"]


def test_every_pdt_module_lifecycle_imports_is_in_lifecycle_modules():
    import ast

    from pdt.deploy_common import LIFECYCLE_MODULES
    pdt_dir = Path(lifecycle.__file__).parent
    pending, seen = ["lifecycle.py"], set()
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        seen.add(name)
        for node in ast.parse((pdt_dir / name).read_text()).body:
            if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "pdt":
                folder = "/".join(node.module.split(".")[1:])
                for alias in node.names:
                    path = f"{folder}/{alias.name}.py" if folder else f"{alias.name}.py"
                    pending.append(path if (pdt_dir / path).exists() else f"{folder}.py")
    assert seen <= set(LIFECYCLE_MODULES)
