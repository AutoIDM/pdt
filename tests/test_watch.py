import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from conftest import plain
from pdt import cli, completion, console, watch

NOW = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)


def entry(outcome="error", hours=0, **fields):
    time = (NOW - timedelta(hours=hours)).strftime(watch.TIME_FORMAT)
    return {"time": time, "project": "/p", "command": "pdt run <arg>", "outcome": outcome,
            "signature": "boom", "interactive": True, **fields}


def lines(history):
    return [json.loads(line) for line in history.read_text().splitlines()] if history.exists() else []


@pytest.fixture
def said(monkeypatch):
    printed = []
    monkeypatch.setattr(watch, "can_offer", lambda: True)
    monkeypatch.setattr(console, "say", lambda message="": printed.append(message))
    return printed


def test_shape_keeps_command_names_and_flags_and_hides_values():
    argv = ["pdt", "run", "my-app", "--since=3d", "--json", "secret"]
    assert watch.shape(argv, {"run"}) == "pdt run <arg> --since=<value> --json <arg>"


def test_signature_removes_values_that_identify_someone(monkeypatch):
    monkeypatch.setenv("MY_TOKEN", "s3cr3tvalue")
    text = ("[bold]failed[/] s3cr3tvalue at https://x.io/a for a@b.com KEY=v /home/u/f "
            "C:\\Users\\u abcdef0123456789abcd after 42 tries\nsecond line")
    assert watch.signature(text) == (
        "failed <value> at <url> for <email> KEY=<value> <path> <path> <id> after # tries")


def test_needs_help_after_three_errors_at_a_terminal_in_24_hours():
    assert watch.needs_help([entry("unhandled")], NOW)
    assert not watch.needs_help([entry("usage"), entry()], NOW)
    assert watch.needs_help([entry("usage", command="pdt deploy"), entry(project="/q"),
                             entry()], NOW)
    assert not watch.needs_help([entry("usage"), entry("interrupted"), entry()], NOW)
    assert not watch.needs_help([entry(hours=25), entry(), entry()], NOW)
    assert not watch.needs_help([entry(interactive=False), entry(), entry()], NOW)


def test_the_command_type_names_the_pdt_command():
    assert watch.command_type("pdt deploy <arg> --yes") == "pdt deploy"
    assert watch.command_type("pdt --verbose run <arg>") == "pdt run"
    assert watch.command_type("pdt <arg> run") == "pdt"


def test_each_error_from_the_third_prints_where_to_get_help(project, monkeypatch, history, said):
    monkeypatch.setattr("sys.argv", ["pdt", "hello-world", "run"])
    for count in range(4):
        with pytest.raises(SystemExit):
            cli.main()
        assert len(said) == max(0, count - 1)
    assert plain(said) == [f"Get help with pdt at {watch.SUPPORT_URL}"] * 2
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


def test_json_output_gets_no_help_line(project, monkeypatch, history):
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


def test_ci_records_nothing(monkeypatch, history):
    monkeypatch.setenv("CI", "true")
    assert watch.watched(lambda: 1) == 1
    assert not history.exists()


def test_an_unhandled_error_prints_where_to_get_help_the_first_time(project, monkeypatch,
                                                                      history, said):
    monkeypatch.setattr("sys.argv", ["pdt", "run", "my-app"])
    assert watch.watched(lambda: 1 / 0, {"run"}) == 1
    assert plain(said) == [f"Get help with pdt run at {watch.SUPPORT_URL}"]


def test_the_signature_hides_paths(project, history):
    def main():
        console.error("no app named 'x' in /home/u/proj")
        return 1
    assert watch.watched(main) == 1
    assert lines(history)[-1]["signature"] == "no app named 'x' in <path>"


def test_the_off_switches_record_and_print_nothing(project, monkeypatch, history, said):
    monkeypatch.setenv(watch.OFF_ENV, "0")
    assert watch.watched(lambda: 1) == 1
    monkeypatch.delenv(watch.OFF_ENV)
    (project / "pdt.yml").write_text("platform:\n  provider: azure\nerror-watch: false\n")
    for _ in range(3):
        assert watch.watched(lambda: 1) == 1
    assert not history.exists() and said == []


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


def test_recent_reads_past_the_tail_to_cover_24_hours(monkeypatch, history):
    monkeypatch.setattr(watch, "TAIL_BYTES", 200)
    now = datetime.now(timezone.utc)
    stamp = now.strftime(watch.TIME_FORMAT)
    rows = [entry(time=stamp)] * 2 + [entry("ok", time=stamp, interactive=False)] * 50
    history.write_text("".join(json.dumps(row) + "\n" for row in rows))
    assert len(watch.recent(now)) == 52
    assert watch.needs_help(watch.recent(now) + [entry(time=stamp)], now)
