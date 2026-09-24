import pytest

from pdt.config import ConfigError, cron_expression, describe_schedule, runs_per_month


@pytest.mark.parametrize("text,expected", [
    ("hourly", "0 * * * *"),
    ("@daily", "0 0 * * *"),
    ("weekly", "0 0 * * 0"),
    ("monthly", "0 0 1 * *"),
    ("yearly", "0 0 1 1 *"),
    ("*/15 * * * *", "*/15 * * * *"),
])
def test_accepted_schedules(text, expected):
    assert cron_expression(text) == expected


@pytest.mark.parametrize("text", [
    "", "sometimes", "0 * * *", "60 * * * *", "* 24 * * *",
    "0 0 32 * *", "0 0 * 13 *", "0 0 * * 8", "*/0 * * * *", "*/ * * * *",
])
def test_rejected_schedules(text):
    with pytest.raises(ConfigError):
        cron_expression(text)


@pytest.mark.parametrize("text,expected", [
    ("hourly", 730.0),
    ("daily", 365 / 12),
    ("monthly", 1.0),
    ("yearly", 1 / 12),
    ("*/15 * * * *", 2920.0),
])
def test_runs_per_month(text, expected):
    assert runs_per_month(text) == pytest.approx(expected, rel=1e-9)


def test_weekly_counts_the_weeks_in_a_year():
    assert runs_per_month("weekly") == pytest.approx(52 / 12, rel=0.02)


def test_day_of_month_and_day_of_week_are_combined_with_or():
    both_restricted = runs_per_month("0 0 1 * 1")
    only_day_of_month = runs_per_month("0 0 1 * *")
    assert both_restricted > only_day_of_month


@pytest.mark.parametrize("text,expected", [
    ("hourly", "every hour at :00"),
    ("15,45 * * * *", "every hour at :15 and :45"),
    ("*/15 * * * *", "every 15 minutes"),
    ("* * * * *", "every minute"),
    ("30 */6 * * *", "every 6 hours at :30"),
    ("daily", "daily at 00:00"),
    ("0 9,17 * * *", "daily at 09:00 and 17:00"),
    ("30 9 * * 1-5", "every Monday, Tuesday, Wednesday, Thursday and Friday at 09:30"),
    ("0 8 * * 0,7", "every Sunday at 08:00"),
    ("monthly", "on day 1 of every month at 00:00"),
    ("0 6 1,15 * *", "on day 1 and 15 of every month at 06:00"),
    ("yearly", "on day 1 of January at 00:00"),
    ("0 0 1 * 1", "cron 0 0 1 * 1"),
    ("*/5 9-17 * * *", "cron */5 9-17 * * *"),
])
def test_schedules_in_plain_words(text, expected):
    assert describe_schedule(text) == expected
