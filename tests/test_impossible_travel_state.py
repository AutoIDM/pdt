from __future__ import annotations

import runpy
from datetime import datetime, timedelta, timezone
from pathlib import Path


RUN = runpy.run_path(
    Path(__file__).parent.parent
    / "src/pdt/examples/impossible-travel-report/run.py")


def test_checkpoint_keeps_a_pair_that_crosses_two_runs():
    first = datetime(2026, 8, 28, 12, tzinfo=timezone.utc)
    second = first + timedelta(minutes=10)
    logins = [
        {"upn": "user@example.com", "name": "", "ts": first,
         "lat": 0, "lon": 0, "ip": "1.1.1.1", "place": "first"},
        {"upn": "user@example.com", "name": "", "ts": second,
         "lat": 50, "lon": 0, "ip": "2.2.2.2", "place": "second"},
    ]
    find_impossible = RUN["find_impossible"]
    assert len(find_impossible(logins, 1000, first + timedelta(minutes=1))) == 1
    assert find_impossible(logins, 1000, second) == []
