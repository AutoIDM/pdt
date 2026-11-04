import json
import zipfile

from service_fixtures import COMMAND, CUSTOMER, token

from pdt import service, service_runner
from pdt.service_delivery import SILENT_SECONDS, Schedule
from pdt.utils import autoidm


def test_with_a_connection_the_timers_are_the_long_ones():
    schedule = Schedule()
    assert schedule.poll_interval(True, 0) == 300 and schedule.checkin_interval(True, 0) == 3600
    assert schedule.poll_interval(False, 0) == 30 and schedule.checkin_interval(False, 0) == 900


def test_the_answer_sets_the_timers():
    schedule = Schedule()
    schedule.apply({"poll_seconds": 5, "ws_checkin_seconds": 7, "min_checkin_seconds": "x"})
    assert (schedule.poll_seconds, schedule.ws_checkin_seconds) == (5, 7)
    assert schedule.min_checkin_seconds == 10


def test_frames_in_the_minimum_interval_give_one_check_in_at_its_end():
    schedule = Schedule()
    schedule.checkin_done(1000.0, False)
    schedule.frames_seen(1)
    schedule.frames_seen(2)
    assert not schedule.checkin_due(True, 1005.0)
    assert schedule.checkin_due(True, 1010.0)
    schedule.checkin_done(1010.0, False)
    assert not schedule.checkin_due(True, 1011.0)


def test_a_changed_doorbell_with_no_frame_means_a_proxy_that_passes_no_frames():
    schedule = Schedule()
    schedule.doorbell_read(200, 4, True, True, 0.0)
    schedule.doorbell_read(200, 4, True, True, 100.0)
    assert schedule.silent_until == 100.0 + SILENT_SECONDS
    assert schedule.poll_interval(True, 200.0) == schedule.poll_seconds


def test_a_404_doorbell_asks_for_nothing_and_a_403_asks_for_a_check_in():
    schedule = Schedule()
    schedule.checkin_done(0.0, False)
    schedule.doorbell_read(404, 0, False, False, 1.0)
    assert not schedule.wanted
    schedule.doorbell_read(403, 0, False, False, 2.0)
    assert schedule.wanted


def test_failed_check_ins_wait_longer_each_time():
    schedule = Schedule()
    schedule.checkin_failed(0.0)
    first = schedule.retry_at
    schedule.checkin_failed(0.0)
    schedule.checkin_failed(0.0)
    assert 0 < first <= 2 and schedule.retry_at >= 4
    assert schedule.wanted
