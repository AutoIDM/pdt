from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import pytest

from conftest import add_app
from pdt import coding_agents, console, support, watch

NOW = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)
SECRET_MESSAGE = "cannot reach payroll.acme.internal as jdoe with s3cr3t-value"


def entry(outcome="error", command="pdt run <value>", hours=0, project="/p", **fields):
    time = (NOW - timedelta(hours=hours)).strftime(watch.TIME_FORMAT)
    return {"time": time, "project": project, "command": command, "outcome": outcome,
            "code": 1, "error": "ConfigError", "where": ["config.py:663 in cron_expression"],
            "message": SECRET_MESSAGE, "pdt": "0.1.9", "os": "win32", "python": "3.12.1",
            **fields}


@pytest.fixture
def quiet(monkeypatch):
    opened, said, asked = [], [], []
    monkeypatch.setattr(support, "check_lines", lambda: ["no problems"])
    monkeypatch.setattr(coding_agents, "installed", lambda: {})
    monkeypatch.setattr(support, "copy", lambda text: True)
    monkeypatch.setattr(support, "can_prompt", lambda interactive: True)
    monkeypatch.setattr(console, "confirm", lambda question: asked.append(question) or True)
    monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url) or True)
    monkeypatch.setattr(console, "bullet", lambda text, indent=2: said.append(text))
    return opened, said, asked


@pytest.fixture
def agent(monkeypatch):
    monkeypatch.setattr(coding_agents, "installed", lambda: {"claude": "/bin/claude"})
    monkeypatch.setattr(coding_agents, "running_inside", lambda: None)
    coding_agents.SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    coding_agents.SETTINGS.write_text("coding-agent-consent: true\n")


def sent(opened):
    query = parse_qs(urlparse(opened[0]).query)
    return query["title"][0], query["body"][0]


def test_last_error_is_the_newest_error_here():
    newest = entry(command="pdt deploy <value>", hours=1)
    entries = [entry(hours=3), newest, entry("ok", "pdt list"), entry(command="pdt support")]
    assert support.last_error(entries, "/p", NOW) is newest


def test_a_later_success_of_the_same_command_shape_does_not_hide_the_error():
    failed = entry(command="pdt deploy <value>", hours=2)
    assert support.last_error([failed, entry("ok", "pdt deploy <value>")], "/p", NOW) is failed


def test_last_error_skips_other_projects_mistakes_and_old_errors():
    assert support.last_error([entry(project="/q")], "/p", NOW) is None
    assert support.last_error([entry("usage"), entry("interrupted")], "/p", NOW) is None
    assert support.last_error([entry(hours=25)], "/p", NOW) is None


def test_a_crash_is_reported():
    crash = entry("unhandled")
    assert support.last_error([crash], "/p", NOW) is crash


def test_a_long_report_still_fits_the_link():
    body = "\n".join(f"line {number} of the report" for number in range(2000))
    link = support.issue_link("a title", body)
    assert len(link.url) <= support.MAX_URL and link.whole is False
    assert parse_qs(urlparse(link.url).query)["body"][0].endswith(support.CUT)
    assert support.issue_link("a title", "short") == support.Link(
        f"{support.ISSUES_URL}?title=a+title&body=short", True)


def test_with_no_agent_the_report_holds_only_the_structure(project, quiet):
    opened, _said, asked = quiet
    (project / "pdt.yml").write_text("platform:\n  provider: aws\n  profile: AcmePayroll\n")
    add_app(project, "payroll-sync")
    add_app(project, "hr-feed", "platform:\n  provider: azure\n")
    support.propose(entry(command="pdt deploy <value> --yes"))
    title, body = sent(opened)
    assert title == "pdt support: pdt deploy <value> --yes failed: ConfigError"
    assert body == "\n".join([
        support.INTRO, "", "## What happened", "Command: `pdt deploy <value> --yes`",
        "Result: error, exit code 1", "Exception: ConfigError", "", "## Where in pdt",
        "config.py:663 in cron_expression", "", "## Versions",
        "pdt 0.1.9, Python 3.12.1, win32, providers of the apps: aws, azure"])
    assert len(asked) == 1


def test_a_refused_consent_runs_no_check_and_asks_no_agent(project, quiet, agent, monkeypatch):
    opened, _said, _asked = quiet
    coding_agents.SETTINGS.write_text("coding-agent-consent: false\n")
    monkeypatch.setattr(support, "check_lines", lambda: pytest.fail("ran pdt validate"))
    monkeypatch.setattr(coding_agents, "ask", lambda *args: pytest.fail("asked an agent"))
    support.propose(entry())
    assert sent(opened)[1].startswith(support.INTRO)


def test_pdt_asks_for_consent_before_it_gathers_context(project, quiet, agent, monkeypatch):
    events = []
    coding_agents.SETTINGS.unlink()
    monkeypatch.setattr(console, "confirm", lambda question: events.append(question) or False)
    monkeypatch.setattr(support, "check_lines", lambda: events.append("validate") or [])
    monkeypatch.setattr(coding_agents, "ask", lambda *args: pytest.fail("asked an agent"))
    support.propose(entry())
    assert events[0].startswith("Do you permit pdt to call your installed coding agent")
    assert "validate" not in events


def test_the_issue_holds_no_instructions_for_a_coding_agent(project, quiet):
    opened, _said, _asked = quiet
    support.propose(entry())
    title, body = sent(opened)
    assert coding_agents.RULES not in title + body and support.REQUEST[:40] not in body


def test_an_agent_writes_what_failed_and_the_user_confirms_it(project, quiet, agent, monkeypatch):
    opened, said, asked = quiet
    requests = []

    def ask(request, context, seconds):
        requests.append(context)
        assert opened == [] and asked == []
        return coding_agents.Reply("Claude Code", "The schedule `<value>` is not a cron line.")
    monkeypatch.setattr(coding_agents, "ask", ask)
    support.propose(entry())
    assert requests[0]["Error message"] == SECRET_MESSAGE
    assert requests[0]["pdt validate"] == "no problems"
    _title, body = sent(opened)
    assert body.startswith(support.AGENT_INTRO.format(agent="Claude Code"))
    assert body.endswith("## What failed\nThe schedule `<value>` is not a cron line.")
    assert "The schedule `<value>` is not a cron line." in said
    assert asked == ["Does this report look right? Start it as a GitHub issue in your browser?"]


def test_the_agent_gets_the_traceback_of_a_crash(project, quiet, agent, monkeypatch):
    contexts = []
    monkeypatch.setattr(coding_agents, "ask", lambda request, context, seconds:
                        contexts.append(context))
    try:
        raise KeyError("x")
    except KeyError as e:
        support.propose(entry("unhandled"), e)
    assert "KeyError: 'x'" in contexts[0]["Traceback"]


@pytest.mark.parametrize("failure", [None, KeyboardInterrupt, PermissionError("settings.yml"),
                                     UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad")])
def test_with_no_agent_reply_pdt_offers_the_structure_report(project, quiet, agent, monkeypatch,
                                                           failure):
    opened, _said, _asked = quiet

    def ask(request, context, seconds):
        if failure is not None:
            raise failure
    monkeypatch.setattr(coding_agents, "ask", ask)
    support.propose(entry())
    _title, body = sent(opened)
    assert body.startswith(support.INTRO) and SECRET_MESSAGE not in body


def test_pdt_says_that_the_link_sends_the_text_to_github(project, quiet, monkeypatch):
    opened, _said, _asked = quiet
    lines = []
    monkeypatch.setattr(console, "say", lambda message="": lines.append(message))
    support.propose(entry())
    assert ("Your browser sends this text to GitHub. GitHub publishes it only when you check "
            "the issue and select Create.") in lines


def test_nothing_opens_when_the_user_says_no(project, quiet, monkeypatch):
    opened, _said, _asked = quiet
    monkeypatch.setattr(console, "confirm", lambda question: False)
    support.propose(entry())
    assert opened == []


def test_with_no_terminal_pdt_opens_nothing_and_says_where_to_report(project, quiet, monkeypatch):
    opened, said, _asked = quiet
    lines = []
    monkeypatch.setattr(support, "can_prompt", lambda interactive: False)
    monkeypatch.setattr(coding_agents, "installed", lambda: pytest.fail("looked for an agent"))
    monkeypatch.setattr(console, "confirm", lambda question: pytest.fail("asked"))
    monkeypatch.setattr(console, "say", lambda message="": lines.append(message))
    assert support.propose(entry()) == 0
    assert opened == [] and support.INTRO in said
    assert any(support.ISSUES_URL in line for line in lines)


def test_no_recent_error_sends_the_user_to_discord(project, quiet, monkeypatch):
    opened, _said, _asked = quiet
    lines = []
    monkeypatch.setattr(console, "say", lambda message="": lines.append(message))
    assert support.main() == 0
    assert opened == [watch.SUPPORT_URL]
    assert "pdt found no recent error to report." in lines


@pytest.mark.parametrize("variable", [agent.session_var for agent in coding_agents.AGENTS])
def test_a_crash_inside_a_coding_agent_starts_no_report(monkeypatch, variable):
    for agent in coding_agents.AGENTS:
        monkeypatch.delenv(agent.session_var, raising=False)
    monkeypatch.setenv(variable, "1")
    monkeypatch.setattr(support, "propose", lambda entry, error: pytest.fail("started"))
    support.crashed(entry("unhandled"), None)


def test_a_pdt_crash_starts_the_report(project, monkeypatch):
    started = []
    monkeypatch.setattr(watch, "can_offer", lambda: True)
    monkeypatch.setattr(support, "crashed", lambda entry, error: started.append((entry, error)))

    def main():
        raise KeyError("x")
    assert watch.watched(main) == 1
    assert started[0][0]["outcome"] == "unhandled" and isinstance(started[0][1], KeyError)


def test_a_failed_command_does_not_start_the_report(project, monkeypatch):
    monkeypatch.setattr(watch, "can_offer", lambda: True)
    monkeypatch.setattr(support, "crashed", lambda entry, error: pytest.fail("started"))
    monkeypatch.setattr(console, "confirm", lambda question: False)
    assert watch.watched(lambda: 1) == 1
