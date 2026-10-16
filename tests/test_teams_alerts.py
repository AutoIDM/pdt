import json
import re
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
from conftest import add_app, plain

from pdt import alert_message, alerts_cli, config, lifecycle, teams

AT = datetime(2026, 10, 13, 14, 2, tzinfo=timezone.utc)
PARAMS = {"project": "contoso-jobs", "app": "sync-users", "provider": "aws"}


@pytest.fixture
def workflow():
    """A local stand-in for a Teams workflow: it records each JSON body and answers 202
    with no body, or `answer` when a test sets it."""
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
    yield f"http://127.0.0.1:{server.server_port}/workflows/0a1b2c3d4e5f/triggers/manual/paths/invoke", seen, answer
    server.shutdown()


def answers(monkeypatch, *replies):
    """Scripted answers for console.ask and console.confirm, in order."""
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


@pytest.mark.parametrize(("entry", "target"), [
    ({"webhook": "0a1b2c3d4e5f", "events": ["job:run-failed"]}, "workflow 0a1b2c3d4e5f"),
    ({"webhook": "0a1b2c3d4e5f", "name": "IT alerts", "events": ["job:run-failed"]}, "IT alerts"),
])
def test_the_ready_card_names_the_project_the_events_and_the_entry(entry, target):
    content = teams.ready_message("contoso-jobs", entry)["attachments"][0]["content"]
    assert content["fallbackText"] == "Success, you're ready!"
    assert content["body"][1]["text"] == (
        "pdt will post here for these events of the project **contoso-jobs**: Run failed.")
    assert content["body"][2]["text"] == f"Test message to {target}, sent by pdt alerts test"
    assert alerts_cli.SETUPS["teams"].describe(entry) == f"Teams {target}"


@pytest.mark.parametrize(("url", "found"), [
    ("https://default1234.d1.environment.api.powerplatform.com:443/powerautomate/automations/"
     "direct/cu/08/workflows/8e3b1f0c2a4d4e6f9a7b5c3d1e2f4a6b/triggers/manual/paths/invoke"
     "?api-version=1&sp=%2Ftriggers%2Fmanual%2Frun&sv=1.0&sig=abc",
     "8e3b1f0c2a4d4e6f9a7b5c3d1e2f4a6b"),
    ("https://prod-12.westus.logic.azure.com:443/workflows/0a1b2c3d/triggers/manual/paths/invoke",
     "0a1b2c3d"),
])
def test_the_webhook_id_is_the_workflow_id_of_the_url(url, found):
    assert teams.webhook_id(url) == found


def test_a_url_with_no_workflow_id_gets_a_stable_hash():
    found = teams.webhook_id("https://example.com/hook")
    assert re.fullmatch("[0-9A-F]{8}", found)
    assert found == teams.webhook_id("https://example.com/hook") == alert_message.url_id(
        "https://example.com/hook")


def test_deliver_posts_the_card_to_the_workflow_of_the_entry(workflow, monkeypatch):
    url, seen, _answer = workflow
    monkeypatch.setenv("TEAMS_WEBHOOK_0A1B2C3D4E5F", url)
    event = lifecycle.Event("app:deploy-failed", AT, PARAMS)
    teams.deliver(event, {"type": "teams", "webhook": "0a1b2c3d4e5f"}, False)
    assert seen == [teams.message(event)]


def test_a_teams_error_raises_with_teams_answer(workflow, monkeypatch):
    url, _seen, answer = workflow
    answer.update(status=400, body="bad card")
    monkeypatch.setenv("TEAMS_WEBHOOK_0A1B2C3D4E5F", url)
    with pytest.raises(RuntimeError, match="Teams answered HTTP 400: bad card"):
        teams.deliver(lifecycle.Event("job:paused", AT, PARAMS), {"webhook": "0a1b2c3d4e5f"},
                      False)


def test_a_missing_workflow_url_names_the_env_var_and_the_fix(project, monkeypatch):
    monkeypatch.delenv("TEAMS_WEBHOOK_0A1B2C3D4E5F", raising=False)
    monkeypatch.delenv("PDT_ENV_JSON", raising=False)
    with pytest.raises(RuntimeError, match="TEAMS_WEBHOOK_0A1B2C3D4E5F is not set; run pdt alerts "
                                           "setup to connect Teams workflow 0a1b2c3d4e5f"):
        teams.deliver(lifecycle.Event("job:paused", AT, PARAMS), {"webhook": "0a1b2c3d4e5f"},
                      False)


def test_a_cloud_job_finds_the_workflow_url_in_its_env_secret(monkeypatch):
    monkeypatch.delenv("TEAMS_WEBHOOK_W1", raising=False)
    monkeypatch.setenv("PDT_ENV_JSON", json.dumps({"TEAMS_WEBHOOK_W1": "https://x.api.powerplatform.com/w"}))
    assert alert_message.secret_value(teams.secret_name({"webhook": "w1"})) == (
        "https://x.api.powerplatform.com/w")


def test_a_teams_entry_takes_no_channel_and_no_other_key(project):
    for key in ("channel", "url"):
        with pytest.raises(config.ConfigError, match=f"unknown key '{key}'; a teams entry takes: "
                                                     "type, events, exclude-events, name, webhook"):
            lifecycle.parse_alerts([{"type": "teams", "webhook": "w1", key: "x",
                                     "events": ["all"]}], "pdt.yml")


def test_the_workflow_url_of_each_teams_entry_is_a_required_env_var_so_deploy_sends_it(project):
    (project / "pdt.yml").write_text("platform:\n  provider: azure\nalerts:\n"
                                     "  - type: teams\n    webhook: 0a1b2c3d4e5f\n    events: [all]\n")
    add_app(project, "report", "env:\n  required: [API_KEY]\n")
    assert config.env_spec(config.merged_app("report"))["required"] == [
        "API_KEY", "TEAMS_WEBHOOK_0A1B2C3D4E5F"]


def test_teams_starts_unchecked_and_setup_writes_pdt_yml_and_env_then_sends_the_test_on_yes(
        project, workflow, monkeypatch):
    url, seen, _answer = workflow
    monkeypatch.delenv("TEAMS_WEBHOOK_0A1B2C3D4E5F", raising=False)
    shown = {}
    questions = []

    def choice(_number, label):
        if questions.count(questions[-1]) == 1 and label.startswith("[x]"):
            shown.setdefault(questions[-1], []).append(label)

    monkeypatch.setattr(alerts_cli.console, "heading", questions.append)
    monkeypatch.setattr(alerts_cli.console, "choice", choice)
    queue = answers(monkeypatch, "1 2", "", "", url, "y", "y")
    assert alerts_cli.setup() == 0
    assert queue == []
    assert shown["Where should pdt send alerts?"] == ["[x] Slack: a message in a Slack channel"]
    assert config.load_yaml(project / "pdt.yml")["alerts"] == [{
        "type": "teams", "webhook": "0a1b2c3d4e5f",
        "events": ["app:deploy-completed", "app:deploy-failed", "job:run-failed"]}]
    assert (project / ".env").read_text() == f"TEAMS_WEBHOOK_0A1B2C3D4E5F={url}\n"
    assert [payload["attachments"][0]["content"]["fallbackText"] for payload in seen] == [
        "Success, you're ready!"]
    assert body(seen[0])[2]["text"] == ("Test message to workflow 0a1b2c3d4e5f, sent by pdt "
                                        "alerts test")


def test_a_host_outside_power_platform_asks_to_use_it_anyway(project, monkeypatch):
    asked = []
    answers(monkeypatch, "1 2", "", "", "https://example.com/hook")
    monkeypatch.setattr(alerts_cli.console, "confirm", lambda text, **_k: asked.append(text) or "anyway" in text)
    assert alerts_cli.setup() == 0
    assert asked[0] == "https://example.com/hook is not a Teams workflow address. Use it anyway?"
    found = alert_message.url_id("https://example.com/hook")
    assert config.load_yaml(project / "pdt.yml")["alerts"][0]["webhook"] == found
    assert (project / ".env").read_text() == f"TEAMS_WEBHOOK_{found}=https://example.com/hook\n"


def test_enter_at_the_url_prints_the_workflows_steps(project, monkeypatch):
    lines = []
    monkeypatch.setattr(alerts_cli.console, "bullet", lines.append)
    answers(monkeypatch, "1 2", "", "", "", "https://a.environment.api.powerplatform.com/w", "n")
    assert alerts_cli.setup() == 0
    assert len(lines) == 4
    assert lines[0].startswith("1. In Teams, go to the channel for alerts")
    assert lines[1] == "2. Pick the template Send webhook alerts to a channel."


def test_a_url_that_is_not_a_web_address_warns_and_asks_again(project, monkeypatch):
    warned = []
    monkeypatch.setattr(alerts_cli.console, "warn", warned.append)
    answers(monkeypatch, "1 2", "", "", "teams", "https://a.environment.api.powerplatform.com/w", "n")
    assert alerts_cli.setup() == 0
    assert warned == ["A Teams workflow URL starts with https:// and ends in "
                      ".api.powerplatform.com. Press Enter to stop."]


def test_the_default_answer_to_the_test_question_sends_nothing(project, workflow, monkeypatch):
    url, seen, _answer = workflow
    answers(monkeypatch, "1 2", "", "", url, "y", "n")
    assert alerts_cli.setup() == 0
    assert seen == []


SLACK_URL = "https://hooks.slack.com/services/T1/B1/x"
TEAMS_URL = "https://a.environment.api.powerplatform.com/w"
TEAMS_ID = teams.webhook_id(TEAMS_URL)


def asked_in_order(monkeypatch, *replies):
    """Scripted answers; returns each heading, question, and done line in the order setup
    shows them. A KeyboardInterrupt in `replies` is raised in the place of an answer."""
    shown = []
    queue = list(replies)

    def reply(question, *_a, **_k):
        shown.append(question)
        answer = queue.pop(0)
        if answer is KeyboardInterrupt:
            raise KeyboardInterrupt
        return answer

    monkeypatch.setattr(alerts_cli.console, "heading", shown.append)
    monkeypatch.setattr(alerts_cli.console, "done", lambda line: shown.extend(plain([line])))
    monkeypatch.setattr(alerts_cli.console, "ask", reply)
    monkeypatch.setattr(alerts_cli.console, "confirm", lambda question: reply(question) == "y")
    monkeypatch.setattr(alerts_cli, "can_prompt", lambda _interactive: True)
    return shown


def test_setup_finishes_each_type_from_events_to_test_before_the_next_starts(
        project, monkeypatch):
    monkeypatch.delenv("SLACK_WEBHOOK_B1", raising=False)
    monkeypatch.delenv(f"TEAMS_WEBHOOK_{TEAMS_ID.upper()}", raising=False)
    shown = asked_in_order(monkeypatch, "2", "", "", SLACK_URL, "n", "", TEAMS_URL, "n")
    assert alerts_cli.setup() == 0
    boxes = "Type numbers to turn them on or off, or press Enter to go on"
    assert shown == [
        "Where should pdt send alerts?", boxes, "Where should pdt send alerts?", boxes,
        "Slack (1 of 2)", "Which events should go to Slack?", boxes,
        "Paste a webhook URL, or press Enter to make one in your browser",
        "saved  SLACK_WEBHOOK_B1 in .env",
        "saved  the Slack webhook B1 entry under alerts: in pdt.yml",
        "Send a test message to Slack webhook B1 now?",
        "Microsoft Teams (2 of 2)", "Which events should go to Microsoft Teams?", boxes,
        "Paste the workflow URL, or press Enter to see how to make one",
        f"saved  TEAMS_WEBHOOK_{TEAMS_ID.upper()} in .env",
        f"saved  the Teams workflow {TEAMS_ID} entry under alerts: in pdt.yml",
        f"Send a test message to Teams workflow {TEAMS_ID} now?", "Next steps:"]


def test_the_first_type_is_in_pdt_yml_and_env_before_the_next_type_asks_anything(
        project, monkeypatch):
    monkeypatch.delenv("SLACK_WEBHOOK_B1", raising=False)
    asked_in_order(monkeypatch, "2", "", "", SLACK_URL, "n", "", TEAMS_URL, "n")
    on_disk = {}

    def heading(text):
        if text == "Microsoft Teams (2 of 2)":
            on_disk["alerts"] = config.load_yaml(project / "pdt.yml")["alerts"]
            on_disk["env"] = (project / ".env").read_text()

    monkeypatch.setattr(alerts_cli.console, "heading", heading)
    assert alerts_cli.setup() == 0
    assert on_disk == {"alerts": [{"type": "slack", "webhook": "B1", "events": [
        "app:deploy-completed", "app:deploy-failed", "job:run-failed"]}],
        "env": f"SLACK_WEBHOOK_B1={SLACK_URL}\n"}


def test_a_stop_in_the_second_type_keeps_the_first_saved_and_the_second_as_written(
        project, monkeypatch):
    monkeypatch.delenv("SLACK_WEBHOOK_B1", raising=False)
    (project / "pdt.yml").write_text("platform:\n  provider: azure\nalerts:\n"
                                     "  - type: teams\n    webhook: w1\n    events: [all]\n")
    asked_in_order(monkeypatch, "1", "", "", SLACK_URL, "n", "", "y", KeyboardInterrupt)
    with pytest.raises(KeyboardInterrupt):
        alerts_cli.setup()
    assert config.load_yaml(project / "pdt.yml")["alerts"] == [
        {"type": "teams", "webhook": "w1", "events": ["all"]},
        {"type": "slack", "webhook": "B1", "events": [
            "app:deploy-completed", "app:deploy-failed", "job:run-failed"]}]
    assert (project / ".env").read_text() == f"SLACK_WEBHOOK_B1={SLACK_URL}\n"


def test_enter_at_every_question_of_two_types_writes_nothing(project, monkeypatch):
    text = ("platform:\n  provider: azure\nalerts:\n"
            "  - type: slack\n    webhook: B1\n    events: [all]  # mine\n"
            "  - type: teams\n    webhook: w1\n    events: [job:run-failed]\n")
    (project / "pdt.yml").write_text(text)
    monkeypatch.setattr(config, "save_root_alerts", lambda _alerts: pytest.fail("saved"))
    shown = asked_in_order(monkeypatch, "", "", "", "", "", "", "")
    assert alerts_cli.setup() == 0
    assert "Replace Teams workflow w1 with a new one?" in shown
    assert (project / "pdt.yml").read_text() == text
    assert not (project / ".env").exists()


def test_alerts_test_sends_the_ready_card_to_a_teams_entry_and_a_slack_entry(
        project, monkeypatch):
    sent = []
    monkeypatch.setattr(alert_message, "post", lambda _url, payload, service: sent.append(
        (service, payload)) or ("ok" if service == "Slack" else ""))
    (project / "pdt.yml").write_text("platform:\n  provider: azure\nalerts:\n"
                                     "  - type: slack\n    channel: ops\n    events: [all]\n"
                                     "  - type: teams\n    webhook: w1\n    events: [all]\n")
    monkeypatch.setenv("SLACK_WEBHOOK_OPS", "https://hooks.slack.com/x")
    monkeypatch.setenv("TEAMS_WEBHOOK_W1", "https://a.environment.api.powerplatform.com/w")
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
