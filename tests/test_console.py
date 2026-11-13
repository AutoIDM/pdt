import ast
import io
import os
import re
import subprocess
import sys
import threading
import time
import types
from pathlib import Path

import pytest
from rich.console import Console

from pdt import console, deploy, lifecycle
from pdt.utils import email_auth


def test_labelled_lines_keep_escaped_brackets_literal(capsys):
    console.error(console.escape("policy [y/N]"))
    console.step(f"deploy {console.value('[x]')}")
    console.done(console.escape("saved [1]"))
    out = capsys.readouterr().out
    assert "error: policy [y/N]" in out
    assert "==> deploy [x]" in out
    assert "saved [1]" in out


def test_only_the_value_in_a_message_prints_bold(monkeypatch):
    for options, expected in (
            ({"force_terminal": True, "color_system": "standard"},
             "\x1b[32mDeployed \x1b[0m\x1b[1;32mreport\x1b[0m\x1b[32m.\x1b[0m\n"
             "\x1b[2mChecking \x1b[0m\x1b[1mpdt-a\x1b[0m\x1b[2m...\x1b[0m\n"),
            ({"force_terminal": False}, "Deployed report.\nChecking pdt-a...\n")):
        out = io.StringIO()
        monkeypatch.setattr(console, "_console",
                            Console(file=out, highlight=False, soft_wrap=True, **options))
        console.done(f"Deployed {console.value('report')}.")
        console.status(f"Checking {console.value('pdt-a')}...")
        assert out.getvalue() == expected


def test_a_name_or_id_prints_bold_and_only_the_state_has_colour(monkeypatch):
    out = io.StringIO()
    monkeypatch.setattr(console, "_console", Console(
        file=out, highlight=False, soft_wrap=True, force_terminal=True, color_system="standard"))
    console.name("report")
    console.choice(1, "Prod", "sub-1")
    console.secret_changes([("unchanged", "API_KEY", "a***", "a***")])
    lines = out.getvalue().splitlines()
    assert lines[0] == "  \x1b[1mreport\x1b[0m"
    assert lines[1] == "  \x1b[1;36m1)\x1b[0m Prod  \x1b[1msub-1\x1b[0m"
    assert lines[3] == ("\x1b[2munchanged\x1b[0m  \x1b[1mAPI_KEY\x1b[0m  a***      a***")


def test_progress_returns_to_the_line_start(capsys):
    console.progress("3 / 10 MB")
    assert capsys.readouterr().out == "3 / 10 MB\r"


def test_cost_aligns_amounts_and_totals_them(capsys):
    console.cost([("Lambda: 730 runs", 0.12), ("Secrets Manager", 0.4)],
                 "us-east-1 list prices", "excludes logs")
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "Estimated monthly cost (us-east-1 list prices):"
    assert lines[1] == "  Lambda: 730 runs  $      0.12"
    assert lines[2] == "  Secrets Manager   $      0.40"
    assert lines[3] == "  total             $USD   0.52"
    assert lines[4] == "  excludes logs"



def test_cost_repeats_a_symbol_or_code_that_names_one_currency(capsys):
    console.cost([("Cloud Run", 1.5)], "list prices", currency="GBP")
    console.cost([("Cloud Run", 1.5)], "list prices", currency="CHF")
    lines = capsys.readouterr().out.splitlines()
    assert lines[1:3] == ["  Cloud Run  £   1.50", "  total      £   1.50"]
    assert lines[4:6] == ["  Cloud Run  CHF    1.50", "  total      CHF    1.50"]

def test_choice_and_field_keep_text_literal(capsys):
    console.choice(1, "proj [x]", "detail [y]")
    console.field("Run logs", "https://a/b?c=[1]")
    out = capsys.readouterr().out
    assert "  1) proj [x]  detail [y]" in out
    assert "Run logs: https://a/b?c=[1]" in out


def answering(monkeypatch, *answers):
    queue = list(answers)

    def fake_input(prompt=""):
        if not queue:
            raise EOFError
        return queue.pop(0)

    monkeypatch.setattr("builtins.input", fake_input)
    return queue


@pytest.mark.parametrize("answers, default, expected", [
    (["maybe", "y"], False, True),
    (["maybe", "n"], True, False),
    ([""], False, False),
    ([""], True, True),
    ([" YES "], False, True),
    (["No"], True, False),
])
def test_confirm_asks_again_until_the_answer_is_yes_or_no(monkeypatch, capsys, answers, default,
                                                          expected):
    answering(monkeypatch, *answers)
    assert console.confirm("Go?", default=default) is expected
    out = capsys.readouterr().out
    assert out.count("Go? [Y/n]" if default else "Go? [y/N]") == len(answers)
    assert out.count("Please answer y or n.") == len(answers) - 1


def test_confirm_raises_eof_after_a_wrong_answer(monkeypatch):
    answering(monkeypatch, "maybe")
    with pytest.raises(EOFError):
        console.confirm()


def on_terminal(monkeypatch, **options):
    out = io.StringIO()
    for name, stderr in (("_console", False), ("_errors", True)):
        monkeypatch.setattr(console, name, console.MainThreadConsole(
            file=out, stderr=stderr, force_terminal=True, color_system=None, highlight=False,
            soft_wrap=True, **options))
    return out


@pytest.fixture
def terminal(monkeypatch):
    return on_terminal(monkeypatch, force_interactive=True)


def in_thread(*lines):
    printer = threading.Thread(target=lambda: [console.say(line) for line in lines])
    printer.start()
    printer.join()


@pytest.mark.parametrize("ask", [lambda: console.confirm("Start a run now?"),
                                 lambda: console.ask("Which region"),
                                 lambda: console.read("Deploy to which one? [1-2] "),
                                 lambda: console.secret("Secret access key (hidden): ")])
def test_a_line_from_another_thread_waits_until_the_question_is_answered(monkeypatch, terminal,
                                                                          ask):
    out = terminal

    def fake_input(prompt="", stream=None):
        alert = threading.Thread(target=lambda: (
            console.event_failed("The slack alert could not send app:deploy-completed"),
            console.event("Deployed report", "not delivered to slack:")))
        alert.start()
        alert.join()
        return "y"

    monkeypatch.setattr("builtins.input", fake_input)
    monkeypatch.setattr("getpass.getpass", fake_input)
    ask()
    assert out.getvalue().splitlines()[1:] == [
        "The slack alert could not send app:deploy-completed",
        "[alert] not delivered to slack: Deployed report"]


def test_a_line_waits_until_the_answer_when_output_is_not_a_terminal(monkeypatch):
    out = io.StringIO()
    monkeypatch.setattr(console, "_console", console.MainThreadConsole(file=out, highlight=False))

    def fake_input(prompt=""):
        in_thread("background warning")
        return "y"

    monkeypatch.setattr("builtins.input", fake_input)
    console.confirm()
    assert out.getvalue().splitlines() == ["Proceed? [y/N] ", "background warning"]


def test_a_line_waits_until_a_provider_script_has_asked_and_ended(monkeypatch, terminal):
    def provider(args, env):
        terminal.write("Provider deploy: proceed? [y/N] ")
        in_thread("Parent alert could not send app:deploy-started")
        terminal.write("y\n")
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(deploy, "provider_env", lambda extra, rebuild: {})
    monkeypatch.setattr(deploy, "run_provider", provider)
    assert deploy.dispatch("aws", "deploy", "report", False) == 0
    console.say("Deployed.")
    assert terminal.getvalue().splitlines() == [
        "Provider deploy: proceed? [y/N] y", "Parent alert could not send app:deploy-started",
        "Deployed."]


def test_a_line_from_before_the_question_prints_before_the_prompt(monkeypatch, terminal):
    answering(monkeypatch, "y")
    in_thread("background line")
    console.confirm()
    assert terminal.getvalue().splitlines() == ["background line", "Proceed? [y/N] "]


def test_queued_lines_print_before_a_line_queued_while_they_print(monkeypatch, terminal):
    class First:
        def __rich__(self):
            in_thread("new line 3")
            return "old line 1"

    printer = threading.Thread(target=lambda: (console._console.print(First()),
                                               console.say("old line 2")))
    printer.start()
    printer.join()
    console.drain()
    assert terminal.getvalue().splitlines() == ["old line 1", "old line 2", "new line 3"]


def test_a_spinner_redraws_while_the_main_thread_waits(terminal):
    with console.waiting("waiting: Slack"):
        for _ in range(50):
            if "waiting: Slack" in terminal.getvalue():
                break
            threading.Event().wait(0.05)
        assert "waiting: Slack" in terminal.getvalue()


@pytest.mark.parametrize("stop", [EOFError, KeyboardInterrupt])
def test_a_stopped_question_ends_its_line_before_the_queued_lines(monkeypatch, terminal, stop):
    def fake_input(prompt=""):
        in_thread("Queued alert warning")
        raise stop

    monkeypatch.setattr("builtins.input", fake_input)
    with pytest.raises(stop):
        console.confirm()
    assert terminal.getvalue() == "Proceed? [y/N] \nQueued alert warning\n"


def test_a_stopped_question_ends_its_line_with_nothing_queued(monkeypatch, terminal):
    answering(monkeypatch)
    with pytest.raises(EOFError):
        console.confirm()
    assert terminal.getvalue() == "Proceed? [y/N] \n"


def test_lifecycle_flush_prints_the_queued_lines(terminal):
    in_thread("The slack alert could not send app:deploy-completed")
    lifecycle.flush()
    assert terminal.getvalue() == "The slack alert could not send app:deploy-completed\n"


@pytest.mark.parametrize("end", ["pass", "raise SystemExit(3)", "raise KeyboardInterrupt"])
def test_a_queued_line_prints_when_the_process_exits(end):
    script = ("import threading\n"
              "from pdt import console\n"
              "printer = threading.Thread(target=console.say, args=('late line',))\n"
              "printer.start()\n"
              "printer.join()\n"
              f"{end}\n")
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    assert result.stdout == "late line\n"


def test_a_line_from_another_thread_after_the_exit_drain_prints_at_once():
    script = ("import atexit, threading\n"
              "def late():\n"
              "    printer = threading.Thread(target=console.say, args=('after exit',))\n"
              "    printer.start()\n"
              "    printer.join()\n"
              "atexit.register(late)\n"
              "from pdt import console\n")
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    assert result.stdout == "after exit\n"


def test_a_drain_on_another_thread_prints_nothing_inside_a_question(monkeypatch, terminal):
    def fake_input(prompt=""):
        worker = threading.Thread(target=lambda: (console.say("worker warning"), console.drain()))
        worker.start()
        worker.join()
        return "y"

    monkeypatch.setattr("builtins.input", fake_input)
    console.confirm()
    assert terminal.getvalue().splitlines() == ["Proceed? [y/N] ", "worker warning"]


def test_data_prints_after_the_queued_lines(monkeypatch, terminal):
    monkeypatch.setattr(console, "_data", terminal)
    in_thread("queued warning")
    console.data('{"ok": true}')
    assert terminal.getvalue() == 'queued warning\n{"ok": true}\n'


def test_a_piped_answer_ends_the_question_line():
    script = ("from pdt import console\n"
              "console.confirm()\n"
              "console.say('background warning')\n")
    result = subprocess.run([sys.executable, "-c", script], input="y\n", capture_output=True,
                            text=True)
    assert result.stdout == "Proceed? [y/N] \nbackground warning\n"


def test_a_sign_in_prompt_asks_again_then_takes_its_yes_default(monkeypatch, capsys):
    answering(monkeypatch, "ok", "")
    email_auth._confirm("google", "a@example.com")
    assert capsys.readouterr().out.count("Open Google Gmail sign-in now? [Y/n]") == 2


def test_a_sign_in_prompt_treats_end_of_input_as_no(monkeypatch):
    answering(monkeypatch)
    with pytest.raises(email_auth.OAuthError):
        email_auth._confirm("google", "a@example.com")


def poll(wait):
    for state in ["disabling"] * 3 + ["deleting"] * 5:
        wait.now(state)


def test_a_wait_on_a_terminal_shows_one_spinner_line_and_the_end_line(monkeypatch):
    out = io.StringIO()
    monkeypatch.setattr(console, "_console", Console(file=out, force_terminal=True, width=100))
    with console.wait_on("Batch job queue pdt", "deleted") as wait:
        poll(wait)
        wait.start -= 7
    assert out.getvalue().count("\n") < 5
    last = out.getvalue().rsplit("\x1b[2K", 1)[-1]
    assert re.sub(r"\x1b\[[0-9;]*m", "", last).strip() == "deleted: Batch job queue pdt (7s)"


def test_a_wait_prints_plain_lines_on_a_terminal_that_cannot_animate(monkeypatch):
    monkeypatch.setenv("TERM", "dumb")
    out = io.StringIO()
    monkeypatch.setattr(console, "_console", Console(file=out, force_terminal=True, width=100))
    monkeypatch.setattr(console, "_errors", console._console)
    with console.wait_on("Batch job queue pdt", "deleted") as wait:
        poll(wait)
    assert "\x1b[2K" not in out.getvalue()
    assert out.getvalue().count(": Batch job queue pdt") == 3


def test_a_wait_prints_plain_lines_on_a_terminal_when_the_output_is_json(monkeypatch):
    out = io.StringIO()
    monkeypatch.setattr(console, "_console", Console(file=out, force_terminal=True, width=100))
    monkeypatch.setattr(console, "_errors", console._console)
    monkeypatch.setattr(console, "_data", {})
    with console.wait_on("Batch job queue pdt", "deleted") as wait:
        poll(wait)
    assert "\x1b[2K" not in out.getvalue()
    assert out.getvalue().count(": Batch job queue pdt") == 3


def test_a_wait_without_a_poll_or_with_an_error_prints_no_end_line(monkeypatch):
    out = io.StringIO()
    monkeypatch.setattr(console, "_console", Console(file=out, highlight=False))
    monkeypatch.setattr(console, "_errors", console._console)
    with console.wait_on("Batch job queue pdt", "deleted"):
        pass
    assert out.getvalue() == ""
    with pytest.raises(SystemExit):
        with console.wait_on("Batch job queue pdt", "deleted") as wait:
            wait.now("deleting")
            raise SystemExit(1)
    assert out.getvalue() == "deleting: Batch job queue pdt\n"


def test_a_wait_names_what_it_waits_for_and_counts_its_seconds(monkeypatch):
    out = io.StringIO()
    monkeypatch.setattr(console, "_console", Console(file=out, force_terminal=True, width=100))
    with console.waiting("reading: run list") as wait:
        wait.start -= 7
        wait.now("reading: run list")
        console._console.print("a line")
    assert "reading: run list (7s)" in out.getvalue()
    assert "a line" in out.getvalue()


def test_a_wait_off_a_terminal_prints_one_plain_line_on_stderr_each_time(capsys):
    for _ in range(2):
        with console.waiting("reading: run list") as wait:
            wait.now("waiting: new run in the run list")
        console.say("1  succeeded")
    assert capsys.readouterr() == ("1  succeeded\n1  succeeded\n",
                                   "reading: run list\nreading: run list\n")


def test_a_wait_with_json_output_and_no_terminal_prints_nothing(monkeypatch, capsys):
    monkeypatch.setattr(console, "_data", io.StringIO())
    with console.waiting("reading: run list"):
        pass
    assert capsys.readouterr().err == ""


def test_a_wait_with_once_prints_its_plain_line_only_the_first_time(capsys):
    for _ in range(2):
        with console.waiting("reading: prices from the AWS price list", once=True):
            pass
    assert capsys.readouterr().err == "reading: prices from the AWS price list\n"


def test_the_end_line_of_a_wait_that_is_not_the_result_goes_to_stderr(capsys):
    with console.wait_on("pdt gcloud run jobs executions list", "done", result=False) as wait:
        wait.now("Cloud Run API is not ready yet")
    assert capsys.readouterr() == ("", "Cloud Run API is not ready yet: pdt gcloud run jobs "
                                       "executions list\n"
                                       "done: pdt gcloud run jobs executions list (0s)\n")


def test_a_poll_off_a_terminal_prints_its_state_lines_on_stderr(capsys):
    with console.wait_on("Batch job queue pdt", "deleted") as wait:
        poll(wait)
    assert capsys.readouterr() == ("deleted: Batch job queue pdt (0s)\n",
                                   "disabling: Batch job queue pdt\n"
                                   "deleting: Batch job queue pdt\n")


def test_a_slow_wait_shows_its_line_from_the_start_and_prints_no_end_line(monkeypatch):
    out = io.StringIO()
    monkeypatch.setattr(console, "_console", Console(file=out, highlight=False))
    monkeypatch.setattr(console, "_errors", console._console)
    with console.wait_on("pdt az group delete", "done", "deleting: resource group pdt") as wait:
        wait.now("Azure returned an access error")
    assert out.getvalue().splitlines() == ["deleting: resource group pdt",
                                           "Azure returned an access error: resource group pdt"]


def test_a_menu_moves_its_marker_and_leaves_the_answer_on_the_screen(press, capsys):
    press("up", "down", "down", "down", "down", "x", "up", "enter")
    assert console.choose_one(["Prod", "Test", "Dev"], details=["sub-1", "sub-2", "sub-3"]) == 1
    # Off a terminal, each line that the answer erases prints as an empty line.
    lines = [line for line in capsys.readouterr().out.splitlines() if line]
    assert lines[:3] == ["> Prod  sub-1", "  Test  sub-2", "  Dev  sub-3"]
    assert lines[-1] == "> Test  sub-2"


def test_a_menu_of_boxes_returns_each_box_and_says_when_none_is_on(press, capsys):
    press("space", "down", "space", "enter")
    assert console.choose_many(["Slack", "Email"], [True, False]) == [False, True]
    lines = [line for line in capsys.readouterr().out.splitlines() if line]
    assert lines[:3] == ["  ↑ ↓ move, Space turns on or off, Enter goes on",
                         "> [x] Slack", "  [ ] Email"]
    assert lines[-1] == "  [x] Email"
    press("enter")
    assert console.choose_many(["Slack"], [False]) == [False]
    assert capsys.readouterr().out.split("\n")[-3:] == ["  none", "", ""]


def test_a_menu_longer_than_the_screen_shows_the_part_around_the_marker(press, monkeypatch):
    out = io.StringIO()
    monkeypatch.setattr(console, "_console", console.MainThreadConsole(
        file=out, highlight=False, soft_wrap=True, width=20, height=8))
    press(*["down"] * 6, "enter")
    labels = [f"region-{number}" for number in range(10)]
    assert console.choose_one(labels) == 6
    lines = [line for line in out.getvalue().splitlines() if line]
    assert lines[:5] == ["> region-0", "  region-1", "  region-2", "  region-3", "  6 more below"]
    assert lines[-7:-1] == ["  3 more above", "  region-3", "  region-4", "  region-5",
                            "> region-6", "  3 more below"]


@pytest.mark.parametrize("count,pressed,chosen", [
    (4, ["up", "enter"], 0), (4, ["down"] * 4 + ["enter"], 3),
    (5, ["up", "enter"], 4), (5, ["down"] * 5 + ["enter"], 0),
])
def test_a_menu_of_more_than_4_lines_goes_from_one_end_to_the_other(press, count, pressed, chosen):
    press(*pressed)
    assert console.choose_one([f"line-{number}" for number in range(count)]) == chosen
    press(*pressed[:-1], "space", "enter")
    on = console.choose_many([f"line-{number}" for number in range(count)], [False] * count)
    assert on == [number == chosen for number in range(count)]


def test_a_menu_shows_at_most_10_lines_and_follows_the_marker_to_the_end(press, monkeypatch):
    out = io.StringIO()
    monkeypatch.setattr(console, "_console", console.MainThreadConsole(
        file=out, highlight=False, soft_wrap=True, width=20, height=40))
    press("up", "enter")
    labels = [f"region-{number}" for number in range(30)]
    assert console.choose_one(labels) == 29
    lines = [line for line in out.getvalue().splitlines() if line]
    assert lines[:11] == ["> region-0", *(f"  region-{number}" for number in range(1, 10)),
                          "  20 more below"]
    assert lines[11:-1] == ["  20 more above", *(f"  region-{number}" for number in range(20, 29)),
                            "> region-29"]


@pytest.mark.skipif(os.name == "nt", reason="Windows reads keys with ReadConsoleInputW")
@pytest.mark.parametrize("sent,key", [
    (b"\x1b[A", "up"), (b"\x1bOB", "down"), (b" ", "space"), (b"\r", "enter"), (b"\x7f", "backspace"),
    (b"\x15", "clear"), (b"\x04", "eof"), (b"q", "q"), ("é".encode(), "é"), ("€".encode(), "€"),
    (b"\x1b", ""), (b"\x1b[D", ""), (b"\x1b[1;5C", ""), (b"\x1b[3~", ""), (b"\x1bq", ""), (b"\t", ""),
    # The marks a terminal puts around pasted text.
    (b"\x1b[200~", ""), (b"\x1b[201~", "")])
def test_the_bytes_of_a_key_give_its_name_or_its_character(sent, key):
    read, write = os.pipe()
    os.write(write, sent)
    assert console._posix_key(read) == key
    os.close(write)
    with pytest.raises(EOFError):
        console._posix_key(read)
    os.close(read)


def keys_of(sent, chunks=None, pause=0.01):
    """The keys that `_posix_key` reads from a pipe that holds `sent` and then ends. With
    `chunks`, each of them arrives in its own read, `pause` seconds after the one before."""
    read, write = os.pipe()
    found = []

    def arrive():
        for chunk in chunks or [sent]:
            os.write(write, chunk)
            time.sleep(pause)
        os.close(write)

    thread = threading.Thread(target=arrive)
    thread.start()
    try:
        while True:
            found.append(console._posix_key(read))
    except EOFError:
        thread.join()
        os.close(read)
        return found


@pytest.mark.skipif(os.name == "nt", reason="Windows reads keys with ReadConsoleInputW")
@pytest.mark.parametrize("sent,found", [
    # The input ends inside a sequence.
    (b"\x1b", [""]), (b"\x1b[", [""]), (b"\x1b[1;", [""]), (b"\x1bO", [""]), (b"\xe2\x82", [""]),
    # Esc, `[`, parameter bytes, intermediate bytes, one last byte.
    (b"\x1b[1;5Cx", ["", "x"]), (b"\x1b[0 qx", ["", "x"]), (b"\x1b[Zx", ["", "x"]),
    # A byte that cannot go on the sequence is the next key.
    (b"\x1b[1\r", ["", "enter"]), (b"\x1b[1\x1b[A", ["", "up"]),
    # Esc, `O`, and one byte.
    (b"\x1bOPx", ["", "x"]),
    # Alt with a character, and Alt with an arrow key in iTerm2.
    (b"\x1b1\r", ["", "enter"]), (b"\x1b\x1b[Ax", ["", "x"]), (b"\x1b\x1bOAx", ["", "x"]),
    # F1 of the Linux console.
    (b"\x1b[[Ax", ["", "x"]),
    # A first byte with no character does not take the bytes after it.
    (b"\xe2x", ["", "x"]), (b"\xe2\x82x", ["", "x"]), (b"\x80x", ["", "x"]),(b"\xffab", ["", "a", "b"]),
    # Esc and `O`, or Esc, `[`, `[`, take only a last byte 0x40-0x7e.
    (b"\x1bO\rx", ["", "enter", "x"]), (b"\x1b[[\rx", ["", "enter", "x"]),
])
def test_a_key_ends_where_its_form_ends(sent, found):
    assert keys_of(sent) == found


@pytest.mark.skipif(os.name == "nt", reason="Windows reads keys with ReadConsoleInputW")
def test_a_character_whose_bytes_arrive_in_separate_reads_is_one_key():
    euro = "€".encode()
    assert keys_of(b"", [euro[:1], euro[1:2], euro[2:], b"x"]) == ["€", "x"]


@pytest.mark.skipif(os.name == "nt", reason="Windows reads keys with ReadConsoleInputW")
@pytest.mark.parametrize("chunks,found", [
    # A slow connection brings the rest of a sequence after Esc and `[` or `O` later.
    ([b"\x1b[", b"A", b"x"], ["up", "x"]), ([b"\x1bO", b"B", b"x"], ["down", "x"]),
    ([b"\x1b[1;", b"5C", b"x"], ["", "x"]),
    # The Esc key alone sends nothing after Esc.
    ([b"\x1b", b"x"], ["", "x"]),
])
def test_the_rest_of_a_sequence_can_come_later_than_the_byte_after_esc(chunks, found):
    assert keys_of(b"", chunks, pause=0.08) == found


@pytest.mark.skipif(os.name == "nt", reason="Windows reads keys with ReadConsoleInputW")
@pytest.mark.parametrize("first", [b"\x80", b"\xbf", b"\xc0", b"\xc1", b"\xf5", b"\xff"])
def test_a_byte_that_starts_no_character_is_no_key_at_once(first):
    read, write = os.pipe()
    os.write(write, first)
    later = threading.Timer(0.3, os.write, (write, b"x"))
    later.start()
    start = time.monotonic()
    assert console._posix_key(read) == ""
    assert time.monotonic() - start < 0.2
    later.join()
    assert console._posix_key(read) == "x"
    os.close(write)
    os.close(read)


@pytest.mark.parametrize("console_input", [True, False])
def test_a_windows_arrow_key_is_one_key_and_a_typed_letter_is_that_letter(monkeypatch,
                                                                          console_input):
    # The console events of Up, `à`, `x`, Shift+A, Left, a focus change, Down held for three
    # repeats, Enter, Ctrl+Z, and Ctrl+C: each key press and each key release, as
    # ReadConsoleInputW gives them, with the type, down, repeat, code, and character.
    pressed = [(0x26, "\0", 1), (0x30, "à", 1), (0x58, "x", 1), (0x10, "\0", 1), (0x41, "A", 1),
               (0x25, "\0", 1), None, (0x28, "\0", 3), (0x0d, "\r", 1), (0x5a, "\x1a", 1),
               (0x43, "\x03", 1)]
    events = []
    for key in pressed:
        # FOCUS_EVENT is 0x10, KEY_EVENT is 1.
        events += [(0x10, 0, 0, 0, "\0")] if key is None else [
            (1, 1, key[2], key[0], key[1]), (1, 0, 1, key[0], key[1])]
    modes = []

    def get_mode(handle, mode):
        # ENABLE_VIRTUAL_TERMINAL_INPUT (0x200) is on, as a parent can leave it.
        mode._obj.value = 0x2f7
        return console_input

    def read(handle, record, length, count):
        assert (handle, length) == (7, 1)
        # Mode 0 holds from the first read to the last one, for a console.
        assert modes == ([0] if console_input else [])
        event = record._obj
        (event.type, event.event.down, event.event.repeat, event.event.code,
         event.event.char) = events.pop(0)
        count._obj.value = 1
        return 1

    kernel32 = types.SimpleNamespace(
        GetStdHandle=lambda number: {-10: 7}[number], GetConsoleMode=get_mode,
        SetConsoleMode=lambda handle, mode: modes.append(mode) or 1, ReadConsoleInputW=read)
    monkeypatch.setattr("ctypes.WinDLL", lambda name, use_last_error: kernel32, raising=False)
    monkeypatch.setattr(sys, "platform", "win32")
    with pytest.raises(KeyboardInterrupt):
        with console.keys() as key:
            found = [key() for _ in range(9)]
            key()
    assert found == ["up", "à", "x", "A", "down", "down", "down", "enter", "eof"]
    # The mode the console had comes back after Ctrl+C. Input that is not a console keeps
    # its mode.
    assert modes == ([0, 0x2f7] if console_input else [])


def terminal_console(monkeypatch, **options):
    out = io.StringIO()
    out.isatty = lambda: True
    monkeypatch.setattr(console, "_console", console.MainThreadConsole(
        file=out, highlight=False, soft_wrap=True, force_terminal=True, color_system=None, legacy_windows=False,
        **options))
    return out


def test_a_menu_moves_up_over_its_lines_and_erases_each_one(press, monkeypatch):
    out = terminal_console(monkeypatch, width=40, height=20)
    press("down", "enter")
    assert console.choose_one(["a", "b"]) == 1
    up, erase = "\x1b[1A", "\x1b[2K"
    assert out.getvalue() == (
        "\x1b[?25l" + f"{erase}> a\n{erase}  b\n"
        + up * 2 + f"{erase}  a\n{erase}> b\n"
        + up * 2 + f"{erase}> b\n{erase}\n" + up
        + "\x1b[?25h")


def test_each_line_of_a_menu_fits_a_narrow_terminal(press, monkeypatch):
    out = terminal_console(monkeypatch, width=30, height=6)
    press("down", "down", "down", "enter")
    console.choose_many([f"A long label of box {number}" for number in range(9)], [False] * 9)
    lines = re.sub(r"\x1b\[[0-9?]*[A-Za-z]", "", out.getvalue()).splitlines()
    assert max(len(line) for line in lines) <= 29
    assert "  ↑ ↓ move, Space turns on o…" in lines and "  1 more above" in lines


def test_a_narrow_menu_shortens_the_label_and_keeps_the_detail(press, monkeypatch):
    out = terminal_console(monkeypatch, width=24, height=10)
    press("enter")
    console.choose_one(["United States".ljust(20)] * 2, details=["westus2", "westus3"])
    lines = re.sub(r"\x1b\[[0-9?]*[A-Za-z]", "", out.getvalue()).splitlines()
    assert "> United Stat…  westus2" in lines and "  United Stat…  westus3" in lines


@pytest.mark.skipif(os.name == "nt", reason="Windows has no terminal mode to restore")
def test_keys_gives_the_terminal_back_in_the_mode_it_had(monkeypatch):
    import pty
    import termios
    master, slave = pty.openpty()
    monkeypatch.setattr(sys, "stdin", open(slave, closefd=False))
    before = termios.tcgetattr(slave)
    with pytest.raises(RuntimeError):
        with console.keys():
            assert not termios.tcgetattr(slave)[3] & termios.ICANON
            raise RuntimeError
    after = termios.tcgetattr(slave)
    # The kernel sets PENDIN by itself when a terminal goes back to line mode.
    after[3] &= ~termios.PENDIN
    assert after == before
    os.close(master)
    os.close(slave)


@pytest.mark.skipif(os.name == "nt", reason="Windows reads keys with ReadConsoleInputW")
def test_a_byte_left_from_an_earlier_question_is_not_the_first_key(monkeypatch):
    import pty
    master, slave = pty.openpty()
    monkeypatch.setattr(sys, "stdin", open(slave, closefd=False))
    monkeypatch.setattr(console, "_unread", b"x")
    with console.keys() as key:
        # The terminal shows no key in this mode, so nothing waits to be read on `master`.
        os.write(master, b"q")
        assert key() == "q"
    os.close(master)
    os.close(slave)


STOPPED_CHILD = """
import signal, sys
from pdt import console
if sys.argv[1] == "ignore":
    signal.signal(signal.SIGTSTP, signal.SIG_IGN)
with console.keys() as key:
    sys.stdout.write("ready\\n")
    sys.stdout.flush()
    for _ in range(2):
        sys.stdout.write(f"key {key()}\\n")
        sys.stdout.flush()
sys.stdout.write(f"after {signal.getsignal(signal.SIGTSTP).name}\\n")
"""


def read_until(fd, text):
    """What the terminal shows on `fd` up to and including `text`."""
    import select
    shown = b""
    while text.encode() not in shown:
        assert select.select([fd], [], [], 10)[0], shown
        shown += os.read(fd, 1024)
    return shown.decode()


@pytest.mark.skipif(os.name == "nt", reason="Windows has no Ctrl+Z to stop a process")
@pytest.mark.parametrize("disposition", ["default", "ignore"])
def test_ctrl_z_gives_the_terminal_back_and_fg_takes_it_again(disposition):
    import pty
    import signal
    import termios
    master, slave = pty.openpty()
    # Its own process group, with a parent in another group of the session, so that
    # SIGTSTP stops it.
    child = subprocess.Popen([sys.executable, "-c", STOPPED_CHILD, disposition], stdin=slave,
                             stdout=slave, stderr=slave, process_group=0)
    try:
        read_until(master, "ready")
        assert not termios.tcgetattr(slave)[3] & termios.ICANON
        os.kill(child.pid, signal.SIGTSTP)
        if disposition == "default":
            _, status = os.waitpid(child.pid, os.WUNTRACED)
            assert os.WIFSTOPPED(status)
            assert termios.tcgetattr(slave)[3] & termios.ICANON
            read_until(master, "\x1b[?25h")
            os.kill(child.pid, signal.SIGCONT)
            read_until(master, "key resumed")
            assert not termios.tcgetattr(slave)[3] & termios.ICANON
            os.write(master, b"q")
            assert "\x1b[?25h" not in read_until(master, "after SIG_DFL")
        else:
            os.write(master, b"aq")
            shown = read_until(master, "after SIG_IGN")
            assert "key a" in shown and "key q" in shown and "\x1b[?25h" not in shown
        assert child.wait(10) == 0
        assert termios.tcgetattr(slave)[3] & termios.ICANON
    finally:
        child.kill()
        os.close(master)
        os.close(slave)


TWICE_STOPPED_CHILD = """
import os, sys
from pdt import console
with console.keys() as key:
    sys.stdout.write("ready\\n")
    sys.stdout.flush()
    os.read(int(sys.argv[1]), 1)
    for _ in range(2):
        sys.stdout.write(f"key {key()}\\n")
        sys.stdout.flush()
sys.stdout.write("after\\n")
"""


@pytest.mark.skipif(os.name == "nt", reason="Windows has no Ctrl+Z to stop a process")
def test_two_fg_before_a_key_give_one_resumed():
    import pty
    import signal
    import termios
    master, slave = pty.openpty()
    go_read, go_write = os.pipe()
    child = subprocess.Popen([sys.executable, "-c", TWICE_STOPPED_CHILD, str(go_read)],
                             stdin=slave, stdout=slave, stderr=slave, process_group=0,
                             pass_fds=(go_read,))
    try:
        read_until(master, "ready")
        for _ in range(2):
            os.kill(child.pid, signal.SIGTSTP)
            _, status = os.waitpid(child.pid, os.WUNTRACED)
            assert os.WIFSTOPPED(status)
            # The next stop waits until the terminal has shown this output.
            read_until(master, "\x1b[?25h")
            os.kill(child.pid, signal.SIGCONT)
            # The handler is in place again when the terminal is in cbreak mode again.
            deadline = time.monotonic() + 10
            while termios.tcgetattr(slave)[3] & termios.ICANON:
                assert time.monotonic() < deadline
                time.sleep(0.01)
        os.write(go_write, b"g")
        os.write(master, b"q")
        shown = read_until(master, "after")
        assert shown.count("key resumed") == 1 and "key q" in shown
        assert child.wait(10) == 0
    finally:
        child.kill()
        for fd in (master, slave, go_read, go_write):
            os.close(fd)


@pytest.mark.parametrize("stdin,stdout,menu", [(True, True, True), (True, False, False),
                                               (False, True, False)])
def test_a_menu_needs_a_real_terminal_for_the_input_and_the_output(monkeypatch, stdin, stdout, menu):
    # rich reports a terminal for output in a pipe when FORCE_COLOR is set.
    out = terminal_console(monkeypatch)
    out.isatty = lambda: stdout
    monkeypatch.setattr(sys, "stdin", types.SimpleNamespace(isatty=lambda: stdin))
    assert console.can_menu() is menu
    assert console._at_terminal() is menu


@pytest.mark.parametrize("width,menu", [(19, False), (20, True)])
def test_a_terminal_narrower_than_20_columns_gets_no_menu(monkeypatch, width, menu):
    terminal_console(monkeypatch, width=width)
    monkeypatch.setattr(sys, "stdin", types.SimpleNamespace(isatty=lambda: True))
    assert console.can_menu() is menu


@pytest.mark.parametrize("password,shown", [(False, "us-a\b \bwest 1\b \b\b \b1\n"), (True, "\n")])
def test_a_typed_line_leaves_out_each_key_that_is_no_character(press, capsys, password, shown):
    press("up", "down", "u", "s", "-", "a", "backspace", "", "w", "e", "s", "t", "space", "1",
          "backspace", "backspace", "1", "eof", "enter")
    assert console._typed(password) == "us-west1"
    assert capsys.readouterr().out == shown


def test_ctrl_u_removes_the_typed_line_and_ctrl_d_ends_an_empty_one(press, capsys):
    press("a", "é", "clear", "b", "enter", "eof")
    assert console._typed(False) == "b"
    assert capsys.readouterr().out == "aé\b \b\b \bb\n"
    press("a", "space", "b", "c", "space", "word", "d", "word", "word", "word", "e", "enter", "eof")
    assert console._typed(True) == "e"
    with pytest.raises(EOFError):
        console._typed(False)


def test_input_that_is_not_a_terminal_gets_no_menu(monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO("1\n"))
    assert not console.can_menu()


def test_only_console_reads_an_answer_from_the_user():
    # input() writes its prompt to stderr when stdin and stdout are a terminal,
    # and `meltano invoke` shows stderr only one whole line at a time.
    src = Path(console.__file__).parent
    calls = [(str(path.relative_to(src)), ast.unparse(node.func))
             for path in sorted(src.rglob("*.py")) if "examples" not in path.relative_to(src).parts
             for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
             if isinstance(node, ast.Call) and "input" in (getattr(node.func, "id", ""),
                                                           getattr(node.func, "attr", ""))]
    assert calls == [("console.py", "_console.input")], \
        "ask with console.ask, console.read, console.secret or console.confirm"
