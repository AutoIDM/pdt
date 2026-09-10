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
