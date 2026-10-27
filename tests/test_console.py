import io
import re
import subprocess
import sys
import threading

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
        out.write("y\n")
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
        out.write("y\n")
        return "y"

    monkeypatch.setattr("builtins.input", fake_input)
    console.confirm()
    assert out.getvalue().splitlines() == ["Proceed? [y/N] y", "background warning"]


def test_a_line_waits_until_a_provider_script_has_asked_and_ended(monkeypatch, terminal):
    def provider(args, check, env):
        terminal.write("Provider deploy: proceed? [y/N] ")
        in_thread("Parent alert could not send app:deploy-started")
        terminal.write("y\n")
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(deploy, "provider_env", lambda extra, rebuild: {})
    monkeypatch.setattr(deploy.subprocess, "run", provider)
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

    printer = threading.Thread(target=lambda: (console.styled(First()),
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


def test_a_wait_prints_one_line_for_each_state_change_off_a_terminal(monkeypatch):
    out = io.StringIO()
    monkeypatch.setattr(console, "_console", Console(file=out, highlight=False))
    monkeypatch.setattr(console, "_errors", console._console)
    with console.wait_on("Batch job queue pdt", "deleted") as wait:
        poll(wait)
    assert out.getvalue().splitlines() == ["disabling: Batch job queue pdt",
                                           "deleting: Batch job queue pdt",
                                           "deleted: Batch job queue pdt (0s)"]


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
