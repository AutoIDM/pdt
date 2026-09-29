import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from conftest import add_app
from pdt import bug_report, console


@pytest.fixture(autouse=True)
def machine(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "data"))
    monkeypatch.setattr(bug_report.Path, "home", lambda: Path("/home/alice"))
    monkeypatch.setattr(bug_report.getpass, "getuser", lambda: "alice")
    monkeypatch.setattr(bug_report.socket, "gethostname", lambda: "alice-laptop.corp.lan")


@pytest.fixture
def aws_project(project):
    (project / "pdt.yml").write_text(
        "platform:\n  provider: aws\n  region: us-east-1\n"
        "  account: '123456789012'\n  profile: corp-admin\n")
    (project / ".env").write_text("API_TOKEN=hunter2-value\n")
    add_app(project, "payroll-sync", "platform:\n  subscription: team-sub-name\n")
    return project


def raised(message: str):
    try:
        raise ValueError(message)
    except ValueError as exc:
        return exc, exc.__traceback__


def test_scrub_removes_each_kind_of_private_data(project):
    text = (f"{project}/payroll-sync/run.py "
            "/home/alice/.cache/x C:\\home\\alice\\x "
            "user alice on alice-laptop.corp.lan and alice-laptop "
            "mail alice.smith@example.com "
            "sub 0f8fad5b-d9cb-469f-a165-70867728950e "
            "ip 10.1.2.3 account 123456789012 "
            "token ghp_16C7e42F292c6912E7710c838347Ae178B4a "
            "value hunter2-value "
            "/usr/lib/python3.12/site-packages/pdt/cli.py "
            "the deploy failed because the region was wrong")
    out = bug_report.scrub(text, ["hunter2-value", "payroll-sync"])
    assert "<project>/<redacted>/run.py" in out
    assert "~/.cache/x C:~\\x" in out
    assert "user <user> on <host> and <host>" in out
    assert "mail <email>" in out
    assert "sub <id>" in out
    assert "ip <ip> account <account>" in out
    assert "token <secret>" in out
    assert "value <redacted>" in out
    assert "<site-packages>/pdt/cli.py" in out
    assert "the deploy failed because the region was wrong" in out
    for private in ("alice", "example.com", "10.1.2.3", "hunter2", "payroll", "/usr/lib"):
        assert private not in out


def test_scrub_skips_known_values_shorter_than_four_characters():
    assert bug_report.scrub("the job ran", ["job"]) == "the job ran"


def test_known_secrets_reads_env_values_platform_values_and_app_names(aws_project):
    found = bug_report.known_secrets()
    for value in ("hunter2-value", "123456789012", "corp-admin", "team-sub-name", "payroll-sync"):
        assert value in found
    assert "aws" not in found
    assert "us-east-1" not in found


def test_known_secrets_outside_a_project_is_empty(tmp_path, monkeypatch):
    monkeypatch.delenv("PDT_PROJECT", raising=False)
    monkeypatch.chdir(tmp_path)
    assert bug_report.known_secrets() == []


def test_build_report_hides_argument_values_and_keeps_flags(aws_project, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["pdt", "secrets", "payroll-sync", "save", "--yes",
                                      "--since=2d"])
    title, body = bug_report.build_report(*raised("hunter2-value was rejected"))
    assert title == "pdt secrets: ValueError"
    assert "`pdt secrets <value> save --yes --since=<value>`" in body
    assert "- Provider: aws" in body
    assert "ValueError: <redacted> was rejected" in body
    assert "payroll-sync" not in body
    assert "hunter2" not in body


def test_issue_url_fits_a_browser_and_keeps_the_error_line():
    trace = "\n".join(f'  File "x.py", line {n}, in step_{n}' for n in range(3000))
    body = f"head\n{bug_report.TRACE_START}{trace}\nValueError: last{bug_report.TRACE_END}"
    url = bug_report.issue_url("pdt deploy: ValueError", body)
    assert len(url) < 7000
    sent = parse_qs(urlparse(url).query)["body"][0]
    assert sent.startswith("head\n")
    assert "earlier lines removed" in sent
    assert sent.endswith("step_2999\nValueError: last\n```\n")


def test_hook_ignores_keyboard_interrupt(monkeypatch):
    offered = []
    monkeypatch.setattr(bug_report, "offer", lambda *report: offered.append(report))
    bug_report.hook(KeyboardInterrupt, KeyboardInterrupt(), None)
    assert offered == []


def test_hook_never_raises(monkeypatch):
    def broken(*_):
        raise RuntimeError("report failed")
    monkeypatch.setattr(bug_report, "build_report", broken)
    exc, tb = raised("boom")
    bug_report.hook(ValueError, exc, tb)


def test_hook_without_a_terminal_saves_the_report_and_asks_nothing(
        aws_project, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CI", "true")
    monkeypatch.setattr(sys, "argv", ["pdt", "deploy", "payroll-sync"])
    monkeypatch.setattr(console, "confirm", lambda _q: pytest.fail("asked a question"))
    exc, tb = raised("boom")
    bug_report.hook(ValueError, exc, tb)
    saved = list((tmp_path / "data" / "pdt" / "bug-reports").glob("*.md"))
    assert len(saved) == 1
    assert saved[0].read_text().startswith("# pdt deploy: ValueError\n")
    out = capsys.readouterr().out
    assert saved[0].name in out
    assert bug_report.ISSUE_URL in out


def test_hook_opens_the_issue_page_when_the_user_says_yes(aws_project, monkeypatch, capsys):
    opened = []
    monkeypatch.setattr(bug_report, "can_prompt", lambda _interactive: True)
    monkeypatch.setattr(console, "confirm", lambda _q: True)
    monkeypatch.setattr(bug_report.webbrowser, "open", opened.append)
    monkeypatch.setattr(sys, "argv", ["pdt", "deploy", "payroll-sync"])
    exc, tb = raised("boom")
    bug_report.hook(ValueError, exc, tb)
    assert len(opened) == 1
    assert opened[0].startswith(bug_report.ISSUE_URL + "?")
    assert opened[0] in capsys.readouterr().out
