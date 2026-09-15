import sys

from pdt.utils import log


def test_non_tty_prints_plain_text(capsys, monkeypatch):
    monkeypatch.setenv("LOG_FORMAT", "text")
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    log.log("info", "starting")
    out = capsys.readouterr().out
    assert "\x1b" not in out
    assert f"{'INFO':<7}" in out


def test_tty_colours_the_level_word(capsys, monkeypatch):
    monkeypatch.setenv("LOG_FORMAT", "text")
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    log.log("info", "starting")
    info_out = capsys.readouterr().out
    assert log.LEVEL_COLOURS["INFO"] in info_out

    log.log("warning", "careful")
    warning_out = capsys.readouterr().out
    assert log.LEVEL_COLOURS["WARNING"] in warning_out
    assert log.LEVEL_COLOURS["WARNING"] != log.LEVEL_COLOURS["INFO"]

    log.log("error", "broken")
    error_out = capsys.readouterr().out
    assert log.LEVEL_COLOURS["ERROR"] in error_out
    assert log.LEVEL_COLOURS["ERROR"] != log.LEVEL_COLOURS["INFO"]
