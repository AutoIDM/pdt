"""close_stale_drafts.py: what counts as a person touching a Draft MR."""

import importlib.util
import sys
from datetime import date
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "close_stale_drafts.py"

spec = importlib.util.spec_from_file_location("close_stale_drafts", SCRIPT)
stale = importlib.util.module_from_spec(spec)
sys.modules["close_stale_drafts"] = stale
spec.loader.exec_module(stale)

BOT = 999
PERSON = 7
TODAY = date(2026, 9, 15)  # a Tuesday


def mr(**overrides):
    base = {"iid": 5, "title": "Draft: thing", "draft": True,
            "created_at": "2026-08-03T09:00:00.000Z"}
    return {**base, **overrides}


def commit(authored, committed=None):
    return {"authored_date": authored, "committed_date": committed or authored}


def note(author_id, when, system=False):
    return {"author": {"id": author_id}, "updated_at": when, "system": system}


def test_business_days_skip_weekends():
    friday, monday = date(2026, 9, 11), date(2026, 9, 14)
    assert stale.business_days_since(friday, monday) == 1
    assert stale.business_days_since(friday, TODAY) == 2
    assert stale.business_days_since(TODAY, TODAY) == 0


def test_opening_the_mr_counts():
    assert stale.last_person_activity(mr(), [], [], BOT) == date(2026, 8, 3)


def test_rebase_by_ci_does_not_count():
    """A rebase pushed by the bot rewrites the committer date and leaves a
    system note by the bot. Neither keeps the Draft alive."""
    commits = [commit("2026-08-04T10:00:00Z", committed="2026-09-14T10:00:00Z")]
    notes = [note(BOT, "2026-09-14T10:00:05Z", system=True),
             note(BOT, "2026-09-14T10:00:06Z")]
    assert stale.last_person_activity(mr(), commits, notes, BOT) == date(2026, 8, 4)


def test_a_person_pushing_or_commenting_counts():
    commits = [commit("2026-08-04T10:00:00Z")]
    notes = [note(BOT, "2026-09-14T10:00:00Z"),
             note(PERSON, "2026-09-10T15:00:00Z", system=True)]
    assert stale.last_person_activity(mr(), commits, notes, BOT) == date(2026, 9, 10)
    commits.append(commit("2026-09-12T08:00:00Z"))
    assert stale.last_person_activity(mr(), commits, notes, BOT) == date(2026, 9, 12)


def test_note_timestamps_are_read_in_utc():
    notes = [note(PERSON, "2026-09-10T22:00:00-05:00")]
    assert stale.last_person_activity(mr(), [], notes, BOT) == date(2026, 9, 11)


def test_stale_needs_more_than_the_limit_and_a_draft():
    limit = stale.STALE_BUSINESS_DAYS
    assert stale.business_days_since(date(2026, 9, 8), TODAY) == limit
    assert not stale.is_stale(mr(), date(2026, 9, 8), TODAY)
    assert stale.is_stale(mr(), date(2026, 9, 7), TODAY)
    assert not stale.is_stale(mr(draft=False), date(2026, 8, 1), TODAY)


def test_dry_run_default_and_merge_request_pipelines():
    assert stale.dry_run_wanted({})
    assert stale.dry_run_wanted({"DRY_RUN": "False", "CI_PIPELINE_SOURCE": "merge_request_event"})
    assert not stale.dry_run_wanted({"DRY_RUN": "false", "CI_PIPELINE_SOURCE": "schedule"})
