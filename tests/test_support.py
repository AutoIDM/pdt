from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import pytest

from pdt import coding_agents, console, support, watch

NOW = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)


def entry(outcome="error", command="pdt run <arg>", hours=0, project="/p", **fields):
    time = (NOW - timedelta(hours=hours)).strftime(watch.TIME_FORMAT)
    return {"time": time, "project": project, "command": command, "outcome": outcome,
            "code": 1, "signature": "boom", **fields}


@pytest.fixture
def quiet(monkeypatch):
    opened, said = [], []
    monkeypatch.setattr(support, "check_lines", lambda apps: ["no problems"])
    monkeypatch.setattr(coding_agents, "installed", lambda: {})
    monkeypatch.setattr(coding_agents, "env_secrets",
                        lambda: coding_agents.EnvSecrets({"API_KEY"}, {"s3cr3t-value"}))
    monkeypatch.setattr(support, "copy", lambda text: True)
    monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url) or True)
    monkeypatch.setattr(console, "bullet", lambda text, indent=2: said.append(text))
    return opened, said


def test_last_error_is_the_newest_error_here():
    newest = entry(command="pdt deploy <arg>", hours=1)
    entries = [entry(hours=3), newest, entry("ok", "pdt list"), entry(command="pdt support")]
    assert support.last_error(entries, "/p", NOW) is newest


def test_a_later_success_of_the_same_command_shape_does_not_hide_the_error():
    failed = entry(command="pdt deploy <arg>", hours=2)
    assert support.last_error([failed, entry("ok", "pdt deploy <arg>")], "/p", NOW) is failed


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


def test_the_report_names_no_path_or_secret(project, quiet, monkeypatch):
    opened, said = quiet
    monkeypatch.setattr(support, "can_prompt", lambda interactive: True)
    monkeypatch.setattr(console, "confirm", lambda question: True)
    support.propose(entry(project=str(project), signature="401 for s3cr3t-value"))
    query = parse_qs(urlparse(opened[0]).query)
    assert "s3cr3t-value" not in query["body"][0] + query["title"][0]
    assert str(project) not in query["body"][0]
    assert "## What happened" in query["body"][0]
    assert any("<secret removed>" in line for line in said)


def test_the_report_names_no_host_relative_path_app_or_user(project, quiet, monkeypatch):
    opened, _said = quiet
    (project / "pdt.yml").write_text("platform:\n  provider: azure\napps:\n  - name: acme-payroll\n")
    monkeypatch.setattr(support.Path, "home", lambda: support.Path("/Users/jdoe"))
    monkeypatch.setattr(support, "check_lines",
                        lambda apps: ["pdt.yml: app 'acme-payroll' has no directory",
                                      "report-i#: missing required env var TOKEN"])
    (project / "report-i89").mkdir()
    (project / "report-i89" / "run.py").write_text("")
    monkeypatch.setattr(support, "can_prompt", lambda interactive: True)
    monkeypatch.setattr(console, "confirm", lambda question: True)
    support.propose(entry(project=str(project), signature=(
        "cannot reach payroll.internal.corp for finance/reports.csv as jdoe in config.py")))
    query = parse_qs(urlparse(opened[0]).query)
    text = query["body"][0] + query["title"][0]
    for private in ("payroll.internal.corp", "finance/reports.csv", "acme-payroll", "jdoe"):
        assert private not in text
    assert "<host> for <path> as <app> in config.py" in text
    assert "pdt.yml: app '<app>' has no directory" in text
    assert "report-i" not in text and "<app>: missing required env var TOKEN" in text


@pytest.mark.parametrize("text, public", [
    ("cannot reach pdt.internal.corp", "cannot reach <host>"),
    ("cannot reach payroll.internal.sh", "cannot reach <host>"),
    ("no file 'finance reports/payroll.csv'", "no file '<path>'"),
    ("app hr failed", "app <app> failed"),
    ("No module named 'azure.identity'", "No module named 'azure.identity'"),
    ("at config.py and pdt.deploy_common", "at config.py and pdt.deploy_common"),
    ("no job pdt-hr-env", "no job pdt-<app>-env"),
    ("platform.provider is required", "platform.provider is required"),
    ("module Microsoft.Graph.Users is missing", "module Microsoft.Graph.Users is missing"),
])
def test_public_text_drops_private_names_and_keeps_pdt_facts(text, public):
    assert support.public(text, ["hr"]) == public


def test_a_numbered_host_goes_after_the_history_filter():
    assert support.public(watch.signature("cannot reach payroll01.internal.corp"), []) == (
        "cannot reach <host>")


def test_a_broken_apps_list_still_gives_the_private_names(project):
    (project / "pdt.yml").write_text("platform:\n  provider: azure\napps: 123\n")
    assert project.name in support.private_names()


def test_the_report_keeps_the_versions_the_error_happened_with(project, quiet):
    report = support.build(entry(project=str(project), pdt="0.1.9", os="win32", python="3.12.1"))
    assert "with pdt 0.1.9 on win32, Python 3.12.1" in report.sections["What happened"]
    assert report.sections["This computer now"].startswith(f"pdt {watch.version()}")


def test_nothing_opens_when_the_user_says_no(project, quiet, monkeypatch):
    opened, _said = quiet
    monkeypatch.setattr(support, "can_prompt", lambda interactive: True)
    monkeypatch.setattr(console, "confirm", lambda question: False)
    support.propose(entry(project=str(project)))
    assert opened == []


def test_with_no_terminal_pdt_prints_the_link_and_asks_nothing(project, quiet, monkeypatch):
    opened, _said = quiet
    links = []
    monkeypatch.setattr(support, "can_prompt", lambda interactive: False)
    monkeypatch.setattr(console, "confirm", lambda question: pytest.fail("asked"))
    monkeypatch.setattr(console, "command", lambda text, note="", indent=2: links.append(text))
    assert support.propose(entry(project=str(project))) == 0
    assert opened == [] and links[0].startswith(support.ISSUES_URL)


def test_no_recent_error_sends_the_user_to_discord(project, quiet, monkeypatch):
    opened, _said = quiet
    lines = []
    monkeypatch.setattr(support, "can_prompt", lambda interactive: True)
    monkeypatch.setattr(console, "confirm", lambda question: True)
    monkeypatch.setattr(console, "say", lambda message="": lines.append(message))
    assert support.main() == 0
    assert opened == [watch.SUPPORT_URL]
    assert "pdt found no recent error to report." in lines


def test_the_agent_advice_is_labeled_and_ctrl_c_skips_it(quiet, monkeypatch):
    _opened, said = quiet
    headings = []
    monkeypatch.setattr(coding_agents, "installed", lambda: {"claude": "/bin/claude"})
    monkeypatch.setattr(coding_agents, "running_inside", lambda: None)
    monkeypatch.setattr(console, "heading", lambda message: headings.append(message))
    monkeypatch.setattr(coding_agents, "ask", lambda request, context, seconds:
                        coding_agents.Reply("Claude Code", "1. nothing\n2. set REGION"))
    support.advise(support.Report("t", {"pdt": "pdt 1"}))
    assert headings[0].startswith("Advice from Claude Code") and said[-1] == "2. set REGION"

    def interrupted(request, context, seconds):
        raise KeyboardInterrupt
    monkeypatch.setattr(coding_agents, "ask", interrupted)
    support.advise(support.Report("t", {"pdt": "pdt 1"}))

    def unreadable(request, context, seconds):
        raise PermissionError("settings.yml")
    monkeypatch.setattr(coding_agents, "ask", unreadable)
    support.advise(support.Report("t", {"pdt": "pdt 1"}))

    def undecodable(request, context, seconds):
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
    monkeypatch.setattr(coding_agents, "ask", undecodable)
    support.advise(support.Report("t", {"pdt": "pdt 1"}))


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
