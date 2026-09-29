from datetime import datetime, timedelta, timezone

import pytest

from pdt import deploy_snowflake, runs_cli
from pdt.deploy_snowflake import Session

CRON = "0 0 * * *"
RUNS = 365 / 12


def session(region="AWS_US_EAST_1"):
    return Session(conn=object(), account="myorg-myacct", region=region, role="SYSADMIN",
                   user="jon", has_warehouse=True)


def finished(minutes, hours_ago=1):
    started = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc) - timedelta(hours=hours_ago)
    return runs_cli.Run(f"RUN_{hours_ago}", started, started + timedelta(minutes=minutes),
                        "succeeded")


def test_the_estimate_prices_the_pool_and_the_task_at_list_price(monkeypatch):
    monkeypatch.setattr(deploy_snowflake, "rate_sheet", lambda session: {})
    estimate = deploy_snowflake.cost_estimate(session(), CRON, [], None)
    pool, task = estimate.items
    assert "compute pool PDT (CPU_X64_XS)" in pool[0]
    assert "~30 runs" in pool[0]
    assert "5 min assumed" in pool[0]
    assert pool[1] == pytest.approx(RUNS * (300 + 60) / 3600 * 0.06 * 2.00)
    assert task[1] == pytest.approx(RUNS * 10 / 3600 * 0.9 * 2.00)
    assert deploy_snowflake.CONSUMPTION_TABLE in estimate.prices
    assert "AWS_US_EAST_1" in estimate.prices


def test_the_estimate_adds_the_stored_bytes_at_the_storage_price(monkeypatch):
    monkeypatch.setattr(deploy_snowflake, "rate_sheet", lambda session: {})
    estimate = deploy_snowflake.cost_estimate(session(), CRON, [], (3, 2 * 1024 ** 4))
    assert len(estimate.items) == 3
    assert estimate.items[2][1] == pytest.approx(2 * 23.00)


def test_a_region_missing_from_the_table_uses_the_default_prices(monkeypatch):
    monkeypatch.setattr(deploy_snowflake, "rate_sheet", lambda session: {})
    credit, storage, source = deploy_snowflake.prices(session("AWS_MARS_1"))
    assert (credit, storage) == deploy_snowflake.LIST_PRICES["AWS_US_EAST_1"]
    assert "AWS_MARS_1 is not in pdt's table" in source


def test_the_account_rate_sheet_replaces_the_list_prices(monkeypatch):
    monkeypatch.setattr(deploy_snowflake, "rate_sheet",
                        lambda session: {"compute": 3.5, "storage": 20.0})
    estimate = deploy_snowflake.cost_estimate(session(), CRON, [], (1, 1024 ** 4))
    assert "rate sheet" in estimate.prices
    assert estimate.items[1][1] == pytest.approx(RUNS * 10 / 3600 * 0.9 * 3.5)
    assert estimate.items[2][1] == pytest.approx(20.0)


def test_recent_runs_replace_the_assumed_duration(monkeypatch):
    monkeypatch.setattr(deploy_snowflake, "rate_sheet", lambda session: {})
    estimate = deploy_snowflake.cost_estimate(session(), CRON, [finished(2)], None)
    label, amount = estimate.items[0]
    assert "2.0 min avg of recent runs" in label
    assert amount == pytest.approx(RUNS * (120 + 60) / 3600 * 0.06 * 2.00)


def test_the_average_uses_only_the_most_recent_finished_runs():
    found = [finished(1, 1), finished(2, 2), finished(3, 3), finished(60, 4)]
    assert deploy_snowflake.RECENT_RUNS == 3
    assert deploy_snowflake.average_run_seconds(found) == 120


def test_a_running_job_does_not_count_toward_the_average():
    running = runs_cli.Run("RUN_0", datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc), None, "running")
    assert deploy_snowflake.average_run_seconds([running, finished(4)]) == 240
    assert deploy_snowflake.average_run_seconds([running]) is None
    assert deploy_snowflake.average_run_seconds([]) is None
