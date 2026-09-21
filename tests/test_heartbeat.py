import time

from pdt.deploy_common import elapsed_text, heartbeat


def test_elapsed_text_formats_seconds_and_minutes():
    assert elapsed_text(45) == "45s"
    assert elapsed_text(60) == "1m 0s"
    assert elapsed_text(150) == "2m 30s"


def test_heartbeat_prints_nothing_before_the_first_tick(capsys):
    with heartbeat(every=5):
        pass

    assert capsys.readouterr().out == ""


def test_heartbeat_ticks_and_reports_elapsed_time_when_the_body_runs_long(capsys):
    with heartbeat(every=0.05):
        time.sleep(0.2)

    out = capsys.readouterr().out
    assert "still working" in out
    assert "took" in out
