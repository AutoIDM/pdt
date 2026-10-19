import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from pdt import cli, completion, console, watch

NOW = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)


def entry(outcome="error", hours=0, **fields):
    time = (NOW - timedelta(hours=hours)).strftime(watch.TIME_FORMAT)
    return {"time": time, "project": "/p", "command": "pdt run <arg>", "outcome": outcome,
            "signature": "boom", "offered": False, **fields}


def lines(history):
    return [json.loads(line) for line in history.read_text().splitlines()] if history.exists() else []


@pytest.fixture
def asked(monkeypatch):
    questions = []
    monkeypatch.setattr(watch, "can_offer", lambda: True)
    monkeypatch.setattr(console, "confirm", lambda question: questions.append(question) or False)
    return questions


def test_shape_keeps_command_names_and_flags_and_hides_values():
    argv = ["pdt", "run", "my-app", "--since=3d", "--json", "secret"]
    assert watch.shape(argv, {"run"}) == "pdt run <arg> --since=<value> --json <arg>"


def test_signature_removes_values_that_identify_someone(monkeypatch):
    monkeypatch.setenv("MY_TOKEN", "s3cr3tvalue")
    text = ("[bold]failed[/] s3cr3tvalue at https://x.io/a for a@b.com KEY=v /home/u/f "
            "C:\\Users\\u abcdef0123456789abcd after 42 tries\nsecond line")
    assert watch.signature(text) == (
        "failed <value> at <url> for <email> KEY=<value> <path> <path> <id> after # tries")


def test_reason_rules():
    assert watch.reason([entry("unhandled")], NOW) == "an error it did not expect"
    assert watch.reason([entry("usage")] * 3, NOW) == "3 command line mistakes in a row"
    assert watch.reason([entry("usage")] * 2, NOW) == ""
    assert watch.reason([entry()] * 3, NOW) == "the same error 3 times in the last 24 hours"


def test_reason_counts_only_errors_in_the_last_24_hours():
    assert watch.reason([entry(hours=25), entry(), entry()], NOW) == ""


def test_reason_offers_at_most_once_a_day():
    assert watch.reason([entry("unhandled", hours=2, offered=True), entry("unhandled")], NOW) == ""
    assert watch.reason([entry("unhandled", hours=25, offered=True), entry("unhandled")], NOW) != ""


def test_three_command_line_mistakes_ask_on_the_third(project, monkeypatch, history, asked):
    monkeypatch.setattr("sys.argv", ["pdt", "hello-world", "run"])
    for count in range(3):
        with pytest.raises(SystemExit):
            cli.main()
        assert len(asked) == (1 if count == 2 else 0)
    last = lines(history)[-1]
    assert last["command"] == "pdt <arg> run"
    assert last["outcome"] == "usage" and last["code"] == 2 and last["offered"] is True


def test_a_value_typed_in_the_wrong_slot_stays_out_of_the_history(project, monkeypatch, history):
    monkeypatch.setattr("sys.argv", ["pdt", "run", "hello-world", "hunter2", "--since=s3cret"])
    with pytest.raises(SystemExit):
        cli.main()
    last = lines(history)[-1]
    assert "hunter2" not in json.dumps(last) and "s3cret" not in json.dumps(last)
    assert last["signature"].startswith("unrecognized arguments: <arg> --since=<value>")


def test_a_value_joined_to_a_short_flag_stays_out_of_the_history():
    assert watch.shape(["pdt", "run", "-pSecret", "-y"], {"run"}) == "pdt run <arg> -y"
    assert watch.typed_words(["pdt", "run", "-pSecret", "-y"], {"run"}) == ["-pSecret"]


def test_json_output_gets_no_offer(project, monkeypatch, history):
    monkeypatch.setattr("sys.argv", ["pdt", "validate", "--json"])
    monkeypatch.setattr("pdt.utils.email_auth.can_prompt", lambda interactive: True)
    monkeypatch.setattr("sys.stderr.isatty", lambda: True)
    assert watch.can_offer() is False
    monkeypatch.setattr("sys.argv", ["pdt", "validate", "--j"])
    assert watch.can_offer() is False


def test_a_later_handled_note_keeps_an_unhandled_one(tmp_path, monkeypatch):
    note = tmp_path / "n.json"
    monkeypatch.setenv(watch.NOTE_ENV, str(note))
    watch.write_note("unhandled", "KeyError")
    watch.write_note("error", "exit 1")
    assert json.loads(note.read_text())["outcome"] == "unhandled"


def test_a_broken_note_still_records_the_command(monkeypatch, history):
    def main():
        Path(watch.os.environ[watch.NOTE_ENV]).parent.mkdir(parents=True, exist_ok=True)
        Path(watch.os.environ[watch.NOTE_ENV]).write_text("{not json")
        return 1
    assert watch.watched(main) == 1
    assert lines(history)[-1]["signature"] == "exit 1"


def test_a_clipboard_failure_still_opens_the_page(monkeypatch):
    opened = []
    monkeypatch.setattr(console, "confirm", lambda question: True)
    monkeypatch.setattr("shutil.which", lambda name: "/bin/" + name)
    monkeypatch.setattr("subprocess.run", lambda *a, **k: (_ for _ in ()).throw(OSError()))
    monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url) or True)
    watch.offer("a test", {"pdt": "1", "os": "x", "python": "3", "command": "pdt",
                           "outcome": "error", "code": 1, "signature": ""})
    assert opened == [watch.SUPPORT_URL]


def test_ci_records_nothing(monkeypatch, history):
    monkeypatch.setenv("CI", "true")
    assert watch.watched(lambda: 1) == 1
    assert not history.exists()


def test_the_same_error_three_times_asks_on_the_third(project, history, asked):
    def main():
        console.error("no app named 'x' in /home/u/proj")
        return 1
    for _ in range(3):
        assert watch.watched(main) == 1
    assert len(asked) == 1
    assert lines(history)[-1]["signature"] == "no app named 'x' in <path>"


def test_the_off_switches_record_and_ask_nothing(project, monkeypatch, history, asked):
    monkeypatch.setenv(watch.OFF_ENV, "0")
    assert watch.watched(lambda: 1) == 1
    monkeypatch.delenv(watch.OFF_ENV)
    (project / "pdt.yml").write_text("platform:\n  provider: azure\nerror-watch: false\n")
    for _ in range(3):
        assert watch.watched(lambda: 1) == 1
    assert not history.exists() and asked == []


def test_a_child_writes_its_note_and_no_history(tmp_path, monkeypatch, history):
    note = tmp_path / "notes" / "n.json"
    monkeypatch.setenv(watch.NOTE_ENV, str(note))
    assert watch.watched(lambda: console.error("deploy failed") or 1) == 1
    assert json.loads(note.read_text()) == {"outcome": "error", "signature": "deploy failed"}
    assert not history.exists()


def test_an_unwritable_history_keeps_the_exit_code(tmp_path, monkeypatch):
    (tmp_path / "file").write_text("")
    monkeypatch.setattr(watch, "HISTORY", tmp_path / "file" / "history.jsonl")
    assert watch.watched(lambda: 3) == 3


def test_completion_records_nothing(monkeypatch, history):
    monkeypatch.setattr(completion, "COMPLETING", True)
    assert watch.watched(lambda: 1) == 1
    assert not history.exists()


def test_an_unhandled_exception_records_unhandled_and_returns_1(history):
    def main():
        raise ValueError("bad 7")
    assert watch.watched(main) == 1
    last = lines(history)[-1]
    assert last["outcome"] == "unhandled"
    assert last["signature"] == "ValueError: bad #"


def test_every_provider_script_is_watched():
    folder = Path(watch.__file__).parent
    for name in ("aws", "azure", "google_cloud", "windows"):
        assert "sys.exit(watch.watched(main))" in (folder / f"deploy_{name}.py").read_text()
