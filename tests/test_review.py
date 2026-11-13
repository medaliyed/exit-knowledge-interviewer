"""F10: only explicit expert decisions can approve complete, cited drafts."""

from datetime import UTC, datetime

import pytest

from eki.errors import ConflictError
from eki.runbook.models import SECTION_NAMES, Claim, RunbookEntry, Section
from eki.runbook.review import approve, reject

NOW = datetime.now(UTC)


def all_open() -> RunbookEntry:
    return RunbookEntry.model_validate(
        {
            "id": "entry",
            "topic_id": "t",
            "title": "Unknown",
            **{s: Section(open_question="I don't remember") for s in SECTION_NAMES},
        }
    )


def test_all_open_can_be_approved_and_only_once() -> None:
    approved = approve(all_open(), "Nadia", "Nadia", NOW)
    assert (approved.status, approved.approved_by, approved.approved_at) == (
        "approved",
        "Nadia",
        NOW,
    )
    with pytest.raises(ConflictError):
        approve(approved, "Nadia", "Nadia", NOW)
    with pytest.raises(ValueError, match="expert"):
        approve(all_open(), "Someone Else", "Nadia", NOW)


def test_incomplete_or_uncited_cannot_be_approved() -> None:
    for section in (Section(), Section(claims=(Claim(text="Guess"),))):
        with pytest.raises(ValueError):
            approve(all_open().model_copy(update={"steps": section}), "Nadia", "Nadia", NOW)


def test_rejection_needs_reason_and_cannot_be_approved() -> None:
    with pytest.raises(ValueError):
        reject(all_open(), " ")
    rejected = reject(all_open(), "Out of date")
    assert rejected.status == "rejected"
    with pytest.raises(ConflictError):
        approve(rejected, "Nadia", "Nadia", NOW)
