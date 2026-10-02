import pytest

from pdt import cli, settings


def run_cli(monkeypatch, *argv):
    monkeypatch.setattr("sys.argv", ["pdt", *argv])
    return cli.main()


def test_defaults_without_a_file():
    assert not settings.path().exists()
    assert settings.load() == {"usage_stats": True, "install_id": None}


def test_unreadable_file_means_defaults():
    settings.path().parent.mkdir(parents=True)
    settings.path().write_text("usage_stats: [")
    assert settings.load()["usage_stats"] is True


def test_settings_lives_in_the_data_folder(data_home):
    assert settings.path() == data_home / "pdt" / "settings.yml"


def test_usage_stats_off_persists_and_shows(monkeypatch, capsys):
    monkeypatch.delenv("DO_NOT_TRACK")
    assert run_cli(monkeypatch, "settings", "usage-stats", "off") == 0
    assert "usage-stats is now off." in capsys.readouterr().out
    assert settings.load()["usage_stats"] is False
    assert run_cli(monkeypatch, "settings") == 0
    out = capsys.readouterr().out
    assert "usage-stats: off" in out
    assert str(settings.path()) in out


def test_show_defaults_to_on_without_creating_the_file(monkeypatch, capsys):
    monkeypatch.delenv("DO_NOT_TRACK")
    assert run_cli(monkeypatch, "settings") == 0
    assert "usage-stats: on" in capsys.readouterr().out
    assert not settings.path().exists()


@pytest.mark.parametrize("value", ["1", "true"])
def test_do_not_track_shows_off(monkeypatch, capsys, value):
    monkeypatch.setenv("DO_NOT_TRACK", value)
    assert run_cli(monkeypatch, "settings") == 0
    assert "usage-stats: off (DO_NOT_TRACK is set)" in capsys.readouterr().out


def test_do_not_track_zero_does_not_count(monkeypatch):
    monkeypatch.setenv("DO_NOT_TRACK", "0")
    assert not settings.do_not_track()


def test_turning_on_under_do_not_track_says_nothing_is_sent(monkeypatch, capsys):
    assert run_cli(monkeypatch, "settings", "usage-stats", "on") == 0
    assert "DO_NOT_TRACK is set" in capsys.readouterr().out


def test_typo_gets_a_clear_error(monkeypatch, capsys):
    with pytest.raises(SystemExit):
        run_cli(monkeypatch, "settings", "usage-stat", "off")
    assert "invalid choice: 'usage-stat'" in capsys.readouterr().err


def test_saved_file_keeps_its_comment_and_reloads():
    settings.save({"usage_stats": False, "install_id": None})
    text = settings.path().read_text()
    assert text.startswith(settings.HEADER)
    values = settings.load()
    assert values["usage_stats"] is False
    assert len(values["install_id"]) == 36
    settings.save(values)
    assert settings.load() == values
    assert settings.path().read_text() == text
    assert [p.name for p in settings.path().parent.iterdir()] == ["settings.yml"]
