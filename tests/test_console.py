import ast
from pathlib import Path

import pytest

from pdt import console


def test_labelled_lines_keep_brackets_literal(capsys):
    console.error("policy [y/N]")
    console.step("deploy [x]")
    console.done("saved [1]")
    out = capsys.readouterr().out
    assert "error: policy [y/N]" in out
    assert "==> deploy [x]" in out
    assert "saved [1]" in out


def test_progress_returns_to_the_line_start(capsys):
    console.progress("3 / 10 MB")
    assert capsys.readouterr().out == "3 / 10 MB\r"


def test_cost_aligns_amounts_and_totals_them(capsys):
    console.cost([("Lambda: 730 runs", 0.12), ("Secrets Manager", 0.4)],
                 "us-east-1 list prices", "excludes logs")
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "Estimated monthly cost (us-east-1 list prices):"
    assert lines[1] == "  Lambda: 730 runs  $   0.12"
    assert lines[2] == "  Secrets Manager   $   0.40"
    assert lines[3] == "  total             $   0.52"
    assert lines[4] == "  excludes logs"


def test_choice_and_field_keep_text_literal(capsys):
    console.choice(1, "proj [x]", "detail [y]")
    console.field("Run logs", "https://a/b?c=[1]")
    out = capsys.readouterr().out
    assert "  1) proj [x]  detail [y]" in out
    assert "Run logs: https://a/b?c=[1]" in out


@pytest.mark.parametrize("default, answer, expected", [
    (False, "", False), (False, "y", True), (True, "", True), (True, "n", False)])
def test_confirm_prompts_on_stdout(monkeypatch, capsys, default, answer, expected):
    monkeypatch.setattr("builtins.input", lambda prompt="": answer)
    assert console.confirm("Log in now?", default=default) is expected
    captured = capsys.readouterr()
    assert captured.out == f"Log in now? [{'Y/n' if default else 'y/N'}] "
    assert captured.err == ""


def test_ask_prompts_on_stdout(monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", lambda prompt="": " 2 ")
    assert console.ask("Deploy to which one? [1-3]") == "2"
    assert capsys.readouterr() == ("Deploy to which one? [1-3]: ", "")


def test_only_console_calls_input():
    # input() writes its prompt to stderr when stdin and stdout are a terminal,
    # and `meltano invoke` shows stderr only one whole line at a time.
    src = Path(console.__file__).parent
    callers = [str(path.relative_to(src)) for path in src.rglob("*.py")
               if "examples" not in path.parts and path.name != "console.py"
               for node in ast.walk(ast.parse(path.read_text()))
               if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "input"]
    assert callers == [], "prompt with console.ask or console.confirm instead of input()"
