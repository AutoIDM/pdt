import io

from rich.console import Console

from pdt import console


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
