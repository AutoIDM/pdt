import pytest

from conftest import add_app, plain
from pdt import cli, completion, console, deploy, watch

HELP = f"Get help with pdt run at {watch.SUPPORT_URL}"


@pytest.fixture
def said(monkeypatch):
    printed = []
    monkeypatch.setattr(watch, "can_offer", lambda: True)
    monkeypatch.setattr(console, "say", lambda message="": printed.append(message))
    monkeypatch.setattr("sys.argv", ["pdt", "run", "my-app"])
    return printed


def test_the_command_type_names_the_pdt_command():
    assert watch.command_type(["pdt", "deploy", "my-app", "--yes"], {"deploy"}) == "pdt deploy"
    assert watch.command_type(["pdt", "--verbose", "run", "x"], {"run"}) == "pdt run"
    assert watch.command_type(["pdt", "hello-world", "run"], {"run"}) == "pdt"
    assert watch.command_type(["pdt"], {"run"}) == "pdt"


def test_each_failed_command_prints_where_to_get_help_once(project, monkeypatch, said):
    monkeypatch.setattr("sys.argv", ["pdt", "hello-world", "run"])
    for count in range(2):
        with pytest.raises(SystemExit):
            cli.main()
        assert len(said) == count + 1
    assert plain(said) == [f"Get help with pdt at {watch.SUPPORT_URL}"] * 2


def test_a_failed_provider_script_gives_one_help_line(project, monkeypatch, said):
    add_app(project, "hello-world", "schedule: daily\n")
    monkeypatch.setattr("sys.argv", ["pdt", "storage", "hello-world", "ls"])
    monkeypatch.setattr(deploy, "dispatch", lambda *args, **kwargs: 1)
    assert cli.main() == 1
    assert plain(said) == [f"Get help with pdt storage at {watch.SUPPORT_URL}"]


def test_success_interruption_and_a_declined_plan_print_no_help(said):
    for code in (0, 130, deploy.DECLINED):
        assert watch.watched(lambda: code, {"run"}) == code
    assert said == []


def test_a_crash_prints_its_traceback_then_the_help_line(said, capsys):
    def main():
        raise ValueError("bad 7")
    assert watch.watched(main, {"run"}) == 1
    assert "ValueError: bad 7" in capsys.readouterr().err
    assert plain(said) == [HELP]


def test_json_output_and_a_coding_agent_get_no_help_line(project, monkeypatch):
    monkeypatch.setattr("pdt.utils.email_auth.can_prompt", lambda interactive: True)
    monkeypatch.setattr("sys.stderr.isatty", lambda: True)
    monkeypatch.setattr("sys.argv", ["pdt", "validate"])
    assert watch.can_offer() is True
    for flag in ("--json", "--j"):
        monkeypatch.setattr("sys.argv", ["pdt", "validate", flag])
        assert watch.can_offer() is False
    monkeypatch.setattr("sys.argv", ["pdt", "validate"])
    monkeypatch.setenv("CLAUDECODE", "1")
    assert watch.can_offer() is False


def test_no_terminal_gets_no_help_line(monkeypatch):
    monkeypatch.setattr("pdt.utils.email_auth.can_prompt", lambda interactive: True)
    monkeypatch.setattr("sys.stderr.isatty", lambda: False)
    assert watch.can_offer() is False


def test_ci_prints_nothing(monkeypatch, said):
    monkeypatch.setenv("CI", "true")
    assert watch.watched(lambda: 1, {"run"}) == 1
    assert said == []


def test_the_off_switches_print_nothing(project, monkeypatch, said):
    monkeypatch.setenv(watch.OFF_ENV, "0")
    assert watch.watched(lambda: 1, {"run"}) == 1
    monkeypatch.delenv(watch.OFF_ENV)
    (project / "pdt.yml").write_text("platform:\n  provider: azure\nerror-watch: false\n")
    assert watch.watched(lambda: 1, {"run"}) == 1
    assert said == []


def test_completion_prints_nothing(monkeypatch, said):
    monkeypatch.setattr(completion, "COMPLETING", True)
    assert watch.watched(lambda: 1, {"run"}) == 1
    assert said == []
