"""F21 deterministic calendar boundaries and bounded review deadlines."""

from datetime import UTC, date, datetime, timedelta, timezone

import pytest

from eki.runbook.maintenance import deadline, freshness


def test_freshness_includes_the_whole_review_date_and_due_soon_boundary() -> None:
    due = date(2026, 11, 30)
    assert freshness(due, due - timedelta(days=15)) == "current"
    assert freshness(due, due - timedelta(days=14)) == "due_soon"
    assert freshness(due, due) == "due_soon"
    assert freshness(due, due + timedelta(days=1)) == "overdue"


def test_deadline_is_utc_and_bounded() -> None:
    now = datetime(2026, 11, 16, 1, tzinfo=timezone(timedelta(hours=2)))
    assert deadline(now, None) == date(2027, 2, 13)
    assert deadline(now, date(2026, 11, 16)) == date(2026, 11, 16)
    for invalid in (date(2026, 11, 15), date(2027, 11, 16)):
        with pytest.raises(ValueError):
            deadline(now, invalid)
    assert deadline(datetime(2026, 11, 15, tzinfo=UTC), None) == date(2027, 2, 13)
