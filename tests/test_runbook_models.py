"""Deliverable d (F7): the runbook entry template and its status invariants."""

from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from eki.runbook.models import (
    SECTION_NAMES,
    ArtifactCitation,
    Claim,
    EntryStatus,
    InterviewTurnCitation,
    RunbookEntry,
    Section,
)

APPROVED_AT = datetime(2026, 11, 2, 9, 30, tzinfo=UTC)


def cited(text: str) -> Claim:
    return Claim(text=text, citations=(ArtifactCitation(artifact_id="TCK-0001"),))


def entry(**overrides: Any) -> RunbookEntry:
    fields: dict[str, Any] = {
        "id": "RB-001",
        "topic_id": "topic-billing-batch-failures",
        "title": "Recover a failed billing-batch run",
    }
    fields.update(overrides)
    return RunbookEntry(**fields)


def test_section_names_match_f7() -> None:
    assert SECTION_NAMES == (
        "trigger",
        "diagnosis",
        "steps",
        "rollback",
        "contacts",
        "pitfalls",
        "rationale",
    )
    assert set(SECTION_NAMES) <= set(RunbookEntry.model_fields)


def test_new_entry_is_draft() -> None:
    assert entry().status is EntryStatus.DRAFT


def test_status_values() -> None:
    assert {status.value for status in EntryStatus} == {"draft", "approved", "rejected"}


def test_empty_entry_misses_all_sections() -> None:
    assert entry().missing_sections() == SECTION_NAMES


def test_section_with_a_claim_is_not_missing() -> None:
    filled = entry(steps=Section(claims=(cited("Restart the ledger-db replicas"),)))
    assert "steps" not in filled.missing_sections()
    assert filled.section("steps").claims[0].text == "Restart the ledger-db replicas"


def test_open_question_makes_section_not_missing() -> None:
    with_open = entry(rollback=Section(open_question="Expert does not remember the rollback"))
    missing = with_open.missing_sections()
    assert "rollback" not in missing
    assert len(missing) == len(SECTION_NAMES) - 1


def test_citation_kinds_round_trip() -> None:
    original = entry(
        steps=Section(
            claims=(
                Claim(
                    text="Restart the ledger-db replicas",
                    citations=(
                        ArtifactCitation(artifact_id="TCK-0042", quote="restarted replicas"),
                        InterviewTurnCitation(session_id="S-1", turn_index=3),
                    ),
                ),
            )
        )
    )
    restored = RunbookEntry.model_validate(original.model_dump(mode="json"))
    assert restored == original
    kinds = [citation.kind for citation in restored.steps.claims[0].citations]
    assert kinds == ["artifact", "interview_turn"]


def test_unknown_citation_kind_rejected() -> None:
    with pytest.raises(ValidationError):
        Claim.model_validate({"text": "x", "citations": [{"kind": "hearsay", "source": "rumour"}]})


def test_blank_artifact_id_rejected() -> None:
    with pytest.raises(ValidationError):
        ArtifactCitation(artifact_id="   ")


def test_blank_session_id_rejected() -> None:
    with pytest.raises(ValidationError):
        InterviewTurnCitation(session_id=" ", turn_index=0)


def test_whitespace_claim_rejected() -> None:
    with pytest.raises(ValidationError):
        Claim(text=" \n\t")


def test_claim_text_is_stripped() -> None:
    assert Claim(text="  Restart the replicas \n").text == "Restart the replicas"


def test_negative_turn_index_rejected() -> None:
    with pytest.raises(ValidationError):
        InterviewTurnCitation(session_id="S-1", turn_index=-1)


def test_approved_requires_approver_and_time() -> None:
    with pytest.raises(ValidationError, match="approved_by"):
        entry(status="approved")
    with pytest.raises(ValidationError, match="approved_at"):
        entry(status="approved", approved_by="nadia.ferrand")
    approved = entry(status="approved", approved_by="nadia.ferrand", approved_at=APPROVED_AT)
    assert approved.status is EntryStatus.APPROVED


def test_approval_time_must_be_timezone_aware() -> None:
    with pytest.raises(ValidationError):
        entry(
            status="approved",
            approved_by="nadia.ferrand",
            approved_at=datetime(2026, 11, 2, 9, 30),
        )


@pytest.mark.parametrize("status", ["draft", "rejected"])
def test_unapproved_entry_cannot_carry_approval(status: str) -> None:
    with pytest.raises(ValidationError):
        entry(status=status, approved_by="nadia.ferrand", approved_at=APPROVED_AT)


def test_extra_fields_rejected() -> None:
    with pytest.raises(ValidationError):
        entry(severity="high")


def test_entries_are_immutable() -> None:
    with pytest.raises(ValidationError):
        entry().status = EntryStatus.APPROVED  # type: ignore[misc]
