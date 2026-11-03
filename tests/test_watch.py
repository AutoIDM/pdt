import json
from pathlib import Path

import pytest

from conftest import add_app, plain
from pdt import cli, completion, console, deploy, support, watch

REPORT = ", or run pdt support to report it to the pdt team"
HELP = f"Get help with pdt run at {watch.SUPPORT_URL}{REPORT}"


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
    assert plain(said) == [f"Get help with pdt at {watch.SUPPORT_URL}{REPORT}"] * 2


def test_a_failed_provider_script_gives_one_help_line(project, monkeypatch, said):
    add_app(project, "hello-world", "schedule: daily\n")
    monkeypatch.setattr("sys.argv", ["pdt", "storage", "hello-world", "ls"])
    monkeypatch.setattr(deploy, "dispatch", lambda *args, **kwargs: 1)
    assert cli.main() == 1
    assert plain(said) == [f"Get help with pdt storage at {watch.SUPPORT_URL}{REPORT}"]


def test_success_interruption_and_a_declined_plan_print_no_help(said):
    for code in (0, 130, deploy.DECLINED):
        assert watch.watched(lambda: code, {"run"}) == code
    assert said == []


def test_a_crash_prints_its_traceback_then_starts_pdt_support(said, capsys, monkeypatch):
    crashes = []
    monkeypatch.setattr(support, "crashed", lambda entry, error: crashes.append((entry, error)))

    def main():
        raise ValueError("bad 7")
    assert watch.watched(main, {"run"}) == 1
    assert "ValueError: bad 7" in capsys.readouterr().err
    [(entry, error)] = crashes
    assert (entry["outcome"], entry["command"]) == ("unhandled", "pdt run <arg>")
    assert isinstance(error, ValueError)
    assert said == []


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


def lines(history):
    return [json.loads(line) for line in history.read_text().splitlines()] if history.exists() else []


def test_shape_keeps_pdt_command_and_flag_names_and_hides_every_other_word():
    flags = {"--since", "--json", "-y"}
    argv = ["pdt", "run", "my-app", "--since=3d", "--json", "secret"]
    assert watch.shape(argv, {"run"}, flags) == "pdt run <arg> --since=<value> --json <arg>"
    assert watch.shape(["pdt", "run", "-pSecret", "-y"], {"run"}, flags) == "pdt run <arg> -y"
    assert watch.shape(["pdt", "aws", "sts", "--AcmeInternalToken", "--json=x"], {"aws"},
                       flags) == "pdt aws <arg> <arg> --json=<value>"
    assert watch.typed_words(["pdt", "run", "-pSecret", "-y"], {"run"}) == ["-pSecret"]


def test_signature_removes_values_that_identify_someone(monkeypatch):
    monkeypatch.setenv("MY_TOKEN", "s3cr3tvalue")
    text = ("[bold]failed[/] s3cr3tvalue at https://x.io/a for a@b.com KEY=v /home/u/f "
            "C:\\Users\\u abcdef0123456789abcd ACME.INTERNAL after 42 tries\nsecond line")
    assert watch.signature(text) == (
        "failed <value> at <value> for <value> <value> <value> <value> <value> <value> after # "
        "tries")
    assert watch.signature("cannot reach Acme2Payroll", ["Acme2Payroll"]) == "cannot reach <arg>"
    assert watch.signature(watch.signature("deploy failed: 42")) == "deploy failed: #"


def test_each_command_adds_one_history_line_with_no_typed_value(project, monkeypatch):
    monkeypatch.setattr("sys.argv", ["pdt", "run", "hello-world", "hunter2", "--since=s3cret"])
    with pytest.raises(SystemExit):
        cli.main()
    [last] = lines(watch.HISTORY)
    assert "hunter2" not in json.dumps(last) and "s3cret" not in json.dumps(last)
    assert last["outcome"] == "usage"
    assert last["signature"].startswith("unrecognized arguments: <arg> <value>")


def test_a_handled_error_records_the_message_pdt_printed():
    assert watch.watched(lambda: console.error("deploy failed for 3 apps") or 1) == 1
    [last] = lines(watch.HISTORY)
    assert (last["outcome"], last["signature"]) == ("error", "deploy failed for # apps")


def test_an_unhandled_exception_records_unhandled_and_returns_1():
    def main():
        raise ValueError("bad 7")
    assert watch.watched(main) == 1
    assert lines(watch.HISTORY)[-1]["signature"] == "ValueError: bad #"


def test_a_child_writes_its_note_and_no_history(tmp_path, monkeypatch):
    note = tmp_path / "notes" / "n.json"
    monkeypatch.setenv(watch.NOTE_ENV, str(note))
    assert watch.watched(lambda: console.error("deploy failed") or 1) == 1
    assert json.loads(note.read_text()) == {"outcome": "error", "signature": "deploy failed"}
    assert not watch.HISTORY.exists()


def test_a_crash_in_a_child_marks_the_command_unhandled(monkeypatch):
    def main():
        Path(watch.os.environ[watch.NOTE_ENV]).parent.mkdir(parents=True, exist_ok=True)
        watch.write_note("unhandled", "KeyError: x at deploy_aws.py:main")
        watch.write_note("error", "exit 1")
        return 1
    assert watch.watched(main) == 1
    last = lines(watch.HISTORY)[-1]
    assert (last["outcome"], last["signature"]) == ("unhandled", "KeyError: x at deploy_aws.py:main")


def test_a_child_signature_keeps_its_place_and_loses_the_words_the_user_typed(monkeypatch):
    monkeypatch.setattr("sys.argv", ["pdt", "run", "deploy"])

    def main():
        Path(watch.os.environ[watch.NOTE_ENV]).parent.mkdir(parents=True, exist_ok=True)
        watch.write_note("unhandled", watch.signature("deploy failed at deploy_aws.py:main"))
        return 1
    assert watch.watched(main, {"run"}) == 1
    assert lines(watch.HISTORY)[-1]["signature"] == "<arg> failed at deploy_aws.py:main"


def test_the_history_holds_an_id_of_the_project_and_not_its_path(project):
    watch.watched(lambda: 1)
    last = lines(watch.HISTORY)[-1]
    assert str(project) not in json.dumps(last) and project.name not in json.dumps(last)
    assert last["project"] == watch.project() and len(last["project"]) == 16


def test_a_broken_note_still_records_the_command():
    def main():
        Path(watch.os.environ[watch.NOTE_ENV]).parent.mkdir(parents=True, exist_ok=True)
        Path(watch.os.environ[watch.NOTE_ENV]).write_text("{not json")
        return 1
    assert watch.watched(main) == 1
    assert lines(watch.HISTORY)[-1]["signature"] == "exit 1"


def test_an_unwritable_history_keeps_the_exit_code(tmp_path, monkeypatch):
    (tmp_path / "file").write_text("")
    monkeypatch.setattr(watch, "HISTORY", tmp_path / "file" / "history.jsonl")
    assert watch.watched(lambda: 3) == 3


def test_ci_completion_and_the_off_switch_record_nothing(monkeypatch):
    monkeypatch.setenv("CI", "true")
    assert watch.watched(lambda: 1) == 1
    monkeypatch.delenv("CI")
    monkeypatch.setenv(watch.OFF_ENV, "0")
    assert watch.watched(lambda: 1) == 1
    monkeypatch.delenv(watch.OFF_ENV)
    monkeypatch.setattr(completion, "COMPLETING", True)
    assert watch.watched(lambda: 1) == 1
    assert not watch.HISTORY.exists()


def test_every_provider_script_runs_through_the_watch():
    folder = Path(watch.__file__).parent
    for name in ("aws", "azure", "google_cloud", "windows"):
        assert "    run_main(main)" in (folder / f"deploy_{name}.py").read_text()
    assert "code = watch.watched(main)" in (folder / "deploy_common.py").read_text()
