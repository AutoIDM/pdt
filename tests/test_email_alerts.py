import warnings
from datetime import datetime, timezone

import pytest
from conftest import add_app

from pdt import alert_message, alerts_cli, config, email_alert, lifecycle, slack, teams
from pdt.utils import alert, send_email

AT = datetime(2026, 10, 13, 14, 2, tzinfo=timezone.utc)
ENTRY = ("alerts:\n  - type: email\n    method: smtp\n    from: pdt-alerts@example.com\n"
         "    to: [it-ops@example.com]\n    events: [job:run-failed, compliance:changed, "
         "email:send]\n")
SMTP_ENV = {"PDT_SMTP_HOST": "smtp.example.com", "PDT_SMTP_USER": "pdt",
            "PDT_SMTP_PASSWORD": "secret"}


@pytest.fixture
def smtp(monkeypatch):
    """A stand-in for smtplib.SMTP that records each login and each message."""
    sent = []

    class Server:
        def __init__(self, host, port, timeout=None):
            self.at = (host, port)

        def __enter__(self):
            return self

        def __exit__(self, *_error):
            return False

        def starttls(self, context=None):
            pass

        def login(self, user, password):
            self.user = (user, password)

        def send_message(self, message):
            sent.append((self.at, self.user, message))

    monkeypatch.setattr(email_alert.smtplib, "SMTP", Server)
    for name, value in SMTP_ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("PDT_SMTP_PORT", raising=False)
    return sent


def parts(message) -> tuple[str, str]:
    return (message.get_body(("plain",)).get_content(), message.get_body(("html",)).get_content())


def test_a_custom_event_of_an_app_reaches_the_email_type_with_subject_text_and_html(
        project, smtp, monkeypatch, capsys):
    (project / "pdt.yml").write_text("platform:\n  provider: azure\n" + ENTRY)
    monkeypatch.chdir(add_app(project, "compliance"))
    alert.emit("compliance:changed", title="Compliance changed", text="3 devices changed.",
               html="<p>3 devices changed.</p>", url="https://example.com/report", devices=3)
    lifecycle.flush()
    (at, user, message), = smtp
    assert at == ("smtp.example.com", 587) and user == ("pdt", "secret")
    assert (message["From"], message["To"], message["Subject"]) == (
        "pdt-alerts@example.com", "it-ops@example.com", "Compliance changed")
    assert parts(message) == ("3 devices changed.\n", "<p>3 devices changed.</p>\n")
    assert "[alert]" not in capsys.readouterr().out


def test_a_built_in_event_gets_a_plain_and_an_html_body_from_its_message(project, smtp):
    (project / "pdt.yml").write_text(ENTRY)
    lifecycle.emit("job:run-failed", project="contoso-jobs", app="sync-users", provider="aws",
                   exit_code=1)
    lifecycle.flush()
    message = smtp[0][2]
    text, html = parts(message)
    assert message["Subject"] == "Run failed: sync-users (contoso-jobs)"
    assert text.startswith("Run failed: sync-users\n\nProject: contoso-jobs\nApp: sync-users\n")
    assert "Exit code: 1" in text and "job:run-failed at " in text
    assert "<b>Exit code</b></td><td>1</td>" in html and "<code>job:run-failed</code>" in html


def test_slack_and_teams_show_title_text_and_url_and_never_html_or_a_content_field():
    event = lifecycle.Event("compliance:changed", AT, {
        "app": "compliance", "title": "Compliance changed", "text": "3 devices changed.",
        "html": "<p>secret page</p>", "url": "https://example.com/report", "devices": 3})
    message = alert_message.event_message(event)
    assert (message.title, message.text, message.url) == (
        "Compliance changed", "3 devices changed.", "https://example.com/report")
    assert message.fields == (("App", "compliance"), ("Devices", "3"))
    assert "secret page" not in str(slack.message(event)) + str(teams.message(event))
    assert alert_message.event_sentence(event) == "Compliance changed"


def test_send_email_is_deprecated_and_sends_the_event_to_its_own_recipients(project, smtp,
                                                                           monkeypatch):
    (project / "pdt.yml").write_text(ENTRY)
    monkeypatch.chdir(add_app(project, "report"))
    with pytest.warns(DeprecationWarning, match="pdt.utils.alert.emit"):
        used = send_email.send_email("ignored@example.com", "a@example.com, b@example.com",
                                     "Report", "body", "<p>body</p>")
    lifecycle.flush()
    message = smtp[0][2]
    assert used == "smtp"
    assert (message["From"], message["To"], message["Subject"]) == (
        "pdt-alerts@example.com", "a@example.com, b@example.com", "Report")
    assert parts(message) == ("body\n", "<p>body</p>\n")
    with pytest.warns(DeprecationWarning, match="pdt.utils.alert.emit"):
        assert send_email.pick_transport() == "smtp"


def test_send_email_with_no_email_entry_prints_the_alert_line_and_sends_nothing(
        project, smtp, monkeypatch, capsys):
    monkeypatch.chdir(add_app(project, "report"))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        assert send_email.send_email("a@example.com", ["b@example.com"], "Report", "body") == "stdout"
    lifecycle.flush()
    assert smtp == []
    assert capsys.readouterr().out == "[alert] Report\n"


def test_a_send_failure_is_a_warning_and_the_console_line_and_not_an_exit(project, smtp,
                                                                         monkeypatch, capsys):
    (project / "pdt.yml").write_text(ENTRY)
    monkeypatch.delenv("PDT_SMTP_PASSWORD")
    lifecycle.emit("job:run-failed", app="report")
    lifecycle.flush()
    out, err = capsys.readouterr()
    assert "The email alert could not send job:run-failed: PDT_SMTP_PASSWORD is not set" in err
    assert "[alert] not delivered to email: Run of report failed\n" in out


@pytest.mark.parametrize(("keys", "error"), [
    ("from: a@example.com\n    to: [b@example.com]", "needs method, one of: microsoft-365, smtp"),
    ("method: smtp\n    to: [b@example.com]", "needs from"),
    ("method: smtp\n    from: a@example.com\n    to: b@example.com", "needs to, a list"),
])
def test_an_email_entry_needs_a_known_method_a_from_and_a_to_list(keys, error):
    entry = {"type": "email", "events": ["all"]}
    for line in keys.split("\n"):
        key, _, value = line.strip().partition(": ")
        entry[key] = [value[1:-1]] if value.startswith("[") else value
    with pytest.raises(config.ConfigError, match=f"the email alerts entry {error}"):
        lifecycle.parse_entry(entry, "pdt.yml")


@pytest.mark.parametrize(("method", "required", "optional"), [
    ("smtp", ["PDT_SMTP_HOST", "PDT_SMTP_USER", "PDT_SMTP_PASSWORD"], ["PDT_SMTP_PORT"]),
    ("ses", ["PDT_SES_REGION"], ["PDT_SES_ACCESS_KEY_ID", "PDT_SES_SECRET_ACCESS_KEY"]),
    ("resend", ["PDT_RESEND_API_KEY"], []),
    ("microsoft-365", ["PDT_EMAIL_TENANT_ID", "PDT_EMAIL_CLIENT_ID", "PDT_EMAIL_PRIVATE_KEY_PATH"],
     ["PDT_EMAIL_PRIVATE_KEY_B64"]),
])
def test_every_app_gets_the_env_vars_of_the_method_and_lists_none_of_them(
        project, method, required, optional):
    (project / "pdt.yml").write_text(ENTRY.replace("smtp", method))
    add_app(project, "report")
    app = config.merged_app("report")
    spec = config.env_spec(app)
    assert (spec["required"], spec.get("optional", [])) == (required, optional)
    assert config.found_env_lines(app) == []


def test_a_b64_key_satisfies_the_key_path_of_microsoft_365(project):
    (project / "pdt.yml").write_text(ENTRY.replace("smtp", "microsoft-365"))
    add_app(project, "report")
    values = {"PDT_EMAIL_TENANT_ID": "t", "PDT_EMAIL_CLIENT_ID": "c",
              "PDT_EMAIL_PRIVATE_KEY_B64": "AAAA"}
    assert config.check_env(config.env_spec(config.merged_app("report")), values) == []


def test_microsoft_365_posts_to_send_mail_of_the_sender_with_the_html_body(monkeypatch):
    posted = []
    monkeypatch.setattr(email_alert, "graph_token", lambda values: "token")
    monkeypatch.setattr(alert_message, "post", lambda *args: posted.append(args))
    for name in ("PDT_EMAIL_TENANT_ID", "PDT_EMAIL_CLIENT_ID", "PDT_EMAIL_PRIVATE_KEY_B64"):
        monkeypatch.setenv(name, "x")
    entry = {"type": "email", "method": "microsoft-365", "from": "pdt-alerts@example.com",
             "to": ["it-ops@example.com"]}
    email_alert.send(entry, entry["to"], "Subject", "text", "<p>html</p>")
    (url, payload, _service, headers), = posted
    assert url == "https://graph.microsoft.com/v1.0/users/pdt-alerts%40example.com/sendMail"
    assert headers == {"Authorization": "Bearer token"}
    assert payload == {"message": {
        "subject": "Subject", "body": {"contentType": "HTML", "content": "<p>html</p>"},
        "toRecipients": [{"emailAddress": {"address": "it-ops@example.com"}}]}}


def test_setup_keeps_the_custom_event_names_and_exclude_events_when_the_events_change(
        project, smtp, monkeypatch):
    (project / "pdt.yml").write_text(
        "alerts:\n  - type: email\n    method: smtp\n    from: pdt-alerts@example.com\n"
        "    to: [it-ops@example.com]\n"
        "    events: [job:run-failed, report:ready, compliance:*, app:deploy-*]\n"
        "    exclude-events: [app:deploy-started]\n")
    queue = ["", "6 4", "", "n", "n"]
    monkeypatch.setattr(alerts_cli.console, "ask", lambda *_a, **_k: queue.pop(0))
    monkeypatch.setattr(alerts_cli.console, "confirm", lambda *_a, **_k: queue.pop(0) == "y")
    monkeypatch.setattr(alerts_cli, "can_prompt", lambda _interactive: True)
    assert alerts_cli.setup() == 0
    assert queue == []
    assert config.load_yaml(project / "pdt.yml")["alerts"] == [{
        "type": "email", "from": "pdt-alerts@example.com", "method": "smtp",
        "to": ["it-ops@example.com"],
        "events": ["app:deploy-completed", "app:deploy-failed", "job:run-started",
                   "report:ready", "compliance:*"],
        "exclude-events": ["app:deploy-started"]}]


def test_setup_keeps_an_entry_with_only_custom_events_when_no_event_is_checked(
        project, smtp, monkeypatch):
    (project / "pdt.yml").write_text(
        "alerts:\n  - type: email\n    method: smtp\n    from: pdt-alerts@example.com\n"
        "    to: [it-ops@example.com]\n    events: [job:run-failed, report:ready]\n")
    queue = ["", "6", "", "n", "n"]
    monkeypatch.setattr(alerts_cli.console, "ask", lambda *_a, **_k: queue.pop(0))
    monkeypatch.setattr(alerts_cli.console, "confirm", lambda *_a, **_k: queue.pop(0) == "y")
    monkeypatch.setattr(alerts_cli, "can_prompt", lambda _interactive: True)
    assert alerts_cli.setup() == 0
    assert config.load_yaml(project / "pdt.yml")["alerts"][0]["events"] == ["report:ready"]


def test_setup_asks_for_email_writes_pdt_yml_and_env_and_test_sends_to_each_entry(
        project, smtp, monkeypatch):
    for name in SMTP_ENV:
        monkeypatch.delenv(name)
    queue = ["3 1", "", "", "it-ops@example.com, lead@example.com", "1", "pdt-alerts@example.com",
             "smtp.example.com", "", "pdt", "secret", "n"]
    monkeypatch.setattr(alerts_cli.console, "ask", lambda *_a, **_k: queue.pop(0))
    monkeypatch.setattr(alerts_cli.console, "secret", lambda *_a, **_k: queue.pop(0))
    monkeypatch.setattr(alerts_cli.console, "confirm", lambda *_a, **_k: queue.pop(0) == "y")
    monkeypatch.setattr(alerts_cli, "can_prompt", lambda _interactive: True)
    assert alerts_cli.setup() == 0
    assert queue == []
    assert config.load_yaml(project / "pdt.yml")["alerts"] == [{
        "type": "email", "method": "smtp", "from": "pdt-alerts@example.com",
        "to": ["it-ops@example.com", "lead@example.com"],
        "events": ["app:deploy-completed", "app:deploy-failed", "job:run-failed"]}]
    assert (project / ".env").read_text() == (
        "PDT_SMTP_HOST=smtp.example.com\nPDT_SMTP_USER=pdt\nPDT_SMTP_PASSWORD=secret\n")
    assert alerts_cli.test() == 0
    message = smtp[0][2]
    assert message["To"] == "it-ops@example.com, lead@example.com"
    assert message["Subject"] == "Success, you're ready!"
