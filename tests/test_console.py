import io
import re

import pytest
from rich.console import Console

from pdt import console
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
    with console.wait_on("Batch job queue pdt", "deleted") as wait:
        poll(wait)
    assert out.getvalue().splitlines() == ["Batch job queue pdt: disabling",
                                           "Batch job queue pdt: deleting",
                                           "Batch job queue pdt: deleted (0s)"]


def test_a_wait_on_a_terminal_shows_one_spinner_line_and_the_end_line(monkeypatch):
    out = io.StringIO()
    monkeypatch.setattr(console, "_console", Console(file=out, force_terminal=True, width=100))
    with console.wait_on("Batch job queue pdt", "deleted") as wait:
        poll(wait)
        wait.start -= 7
    assert out.getvalue().count("\n") < 5
    last = out.getvalue().rsplit("\x1b[2K", 1)[-1]
    assert re.sub(r"\x1b\[[0-9;]*m", "", last).strip() == "Batch job queue pdt: deleted (7s)"


def test_a_wait_prints_plain_lines_on_a_terminal_when_the_output_is_json(monkeypatch):
    out = io.StringIO()
    monkeypatch.setattr(console, "_console", Console(file=out, force_terminal=True, width=100))
    monkeypatch.setattr(console, "_data", {})
    with console.wait_on("Batch job queue pdt", "deleted") as wait:
        poll(wait)
    assert "\x1b[2K" not in out.getvalue()
    assert out.getvalue().count("Batch job queue pdt: ") == 3


def test_a_wait_without_a_poll_or_with_an_error_prints_no_end_line(monkeypatch):
    out = io.StringIO()
    monkeypatch.setattr(console, "_console", Console(file=out, highlight=False))
    with console.wait_on("Batch job queue pdt", "deleted"):
        pass
    assert out.getvalue() == ""
    with pytest.raises(SystemExit):
        with console.wait_on("Batch job queue pdt", "deleted") as wait:
            wait.now("deleting")
            raise SystemExit(1)
    assert out.getvalue() == "Batch job queue pdt: deleting\n"
