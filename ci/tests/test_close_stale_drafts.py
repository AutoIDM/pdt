"""close_stale_drafts.py: what counts as a person touching a Draft PR."""

import importlib.util
import sys
from datetime import date
from pathlib import Path

import yaml

SCRIPT = Path(__file__).resolve().parent.parent / "close_stale_drafts.py"
REPO = SCRIPT.parent.parent

spec = importlib.util.spec_from_file_location("close_stale_drafts", SCRIPT)
stale = importlib.util.module_from_spec(spec)
sys.modules["close_stale_drafts"] = stale
spec.loader.exec_module(stale)

BOT = "pdt-bot"
PERSON = "alice"
TODAY = date(2026, 9, 15)  # a Tuesday


def pr(**overrides):
    base = {"number": 5, "title": "thing", "draft": True,
            "created_at": "2026-08-03T09:00:00Z"}
    return {**base, **overrides}


def commit(authored):
    return {"commit": {"author": {"date": authored}}}


def comment(login, when):
    return {"user": {"login": login}, "updated_at": when}


def review(login, when):
    return {"user": {"login": login}, "submitted_at": when}


def test_business_days_skip_weekends():
    friday, monday = date(2026, 9, 11), date(2026, 9, 14)
    assert stale.business_days_since(friday, monday) == 1
    assert stale.business_days_since(friday, TODAY) == 2
    assert stale.business_days_since(TODAY, TODAY) == 0


def test_opening_the_pr_counts():
    assert stale.last_person_activity(pr(), [], [], [], BOT) == date(2026, 8, 3)


def test_the_token_user_and_bots_do_not_count():
    """A label or comment from score-prs, or any bot, leaves no trace."""
    commits = [commit("2026-08-04T10:00:00Z")]
    comments = [comment(BOT, "2026-09-14T10:00:05Z"),
                comment("github-actions[bot]", "2026-09-14T10:00:06Z"),
                comment("dependabot[bot]", "2026-09-14T10:00:07Z")]
    reviews = [review("copilot-pull-request-reviewer[bot]", "2026-09-14T11:00:00Z")]
    assert stale.last_person_activity(pr(), commits, comments, reviews, BOT) == date(2026, 8, 4)


def test_a_person_committing_commenting_or_reviewing_counts():
    commits = [commit("2026-08-04T10:00:00Z")]
    comments = [comment(BOT, "2026-09-14T10:00:00Z"), comment(PERSON, "2026-09-09T15:00:00Z")]
    reviews = [review(PERSON, "2026-09-10T15:00:00Z"), {"user": {"login": PERSON},
                                                        "submitted_at": None}]
    assert stale.last_person_activity(pr(), commits, comments, reviews, BOT) == date(2026, 9, 10)
    commits.append(commit("2026-09-12T08:00:00Z"))
    assert stale.last_person_activity(pr(), commits, comments, reviews, BOT) == date(2026, 9, 12)


def test_timestamps_are_read_in_utc():
    comments = [comment(PERSON, "2026-09-10T22:00:00-05:00")]
    assert stale.last_person_activity(pr(), [], comments, [], BOT) == date(2026, 9, 11)


def test_stale_needs_more_than_the_limit_and_a_draft():
    limit = stale.STALE_BUSINESS_DAYS
    assert stale.business_days_since(date(2026, 9, 8), TODAY) == limit
    assert not stale.is_stale(pr(), date(2026, 9, 8), TODAY)
    assert stale.is_stale(pr(), date(2026, 9, 7), TODAY)
    assert not stale.is_stale(pr(draft=False), date(2026, 8, 1), TODAY)


def test_dry_run_is_the_default():
    assert stale.dry_run_wanted({})
    assert stale.dry_run_wanted({"DRY_RUN": "False "}) is False
    assert stale.dry_run_wanted({"DRY_RUN": "no"})


def test_slack_text_lists_each_closed_pr():
    text = stale.slack_text("AutoIDM/pdt", [pr(html_url="https://github.com/AutoIDM/pdt/pull/5")])
    assert text.splitlines() == [
        "Closed 1 stale Draft PR(s) in AutoIDM/pdt (nobody touched them for more than "
        "5 business days):",
        "• #5 thing https://github.com/AutoIDM/pdt/pull/5"]


def test_workflow_runs_armed_on_its_schedule_and_dry_by_hand():
    flow = yaml.safe_load((REPO / ".github" / "workflows" / "close-stale-drafts.yml").read_text())
    on = flow[True]  # YAML 1.1 reads the bare key on as true
    assert on["schedule"] and on["workflow_dispatch"]["inputs"]["dry_run"]["default"] is True
    env = flow["jobs"]["close-stale-drafts"]["env"]
    assert "inputs.dry_run" in env["DRY_RUN"]
    assert "secrets.SLACK_WEBHOOK_URL" in env["SLACK_WEBHOOK_URL"]
