"""Deliverable e (F9): an uncited claim blocks approval, and the error names the claim."""

from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from eki.runbook.models import (
    SECTION_NAMES,
    ArtifactCitation,
    Claim,
    InterviewTurnCitation,
    RunbookEntry,
    Section,
    SectionName,
    UncitedClaim,
    UncitedClaimError,
    find_uncited_claims,
    validate_citations,
)

APPROVAL = {"approved_by": "nadia.ferrand", "approved_at": datetime(2026, 11, 2, tzinfo=UTC)}
RESTART = "Restart the ledger-db replicas first"


def cited(text: str) -> Claim:
    return Claim(text=text, citations=(ArtifactCitation(artifact_id="TCK-0001"),))


def uncited(text: str) -> Claim:
    return Claim(text=text)


def entry(**overrides: Any) -> RunbookEntry:
    fields: dict[str, Any] = {"id": "RB-001", "topic_id": "topic-1", "title": "Recover billing"}
    fields.update(overrides)
    return RunbookEntry(**fields)


def test_fully_cited_entry_passes() -> None:
    full = entry(**{name: Section(claims=(cited(f"{name} claim"),)) for name in SECTION_NAMES})
    assert find_uncited_claims(full) == ()
    validate_citations(full)


def test_interview_turn_citation_counts() -> None:
    claim = Claim(text=RESTART, citations=(InterviewTurnCitation(session_id="S-1", turn_index=4),))
    validate_citations(entry(steps=Section(claims=(claim,))))


def test_uncited_claim_is_named() -> None:
    draft = entry(steps=Section(claims=(cited("Page the on-call"), uncited(RESTART))))
    with pytest.raises(UncitedClaimError) as excinfo:
        validate_citations(draft)
    assert excinfo.value.claims == (UncitedClaim(section="steps", index=1, text=RESTART),)
    assert str(excinfo.value) == f'uncited claim in steps[1]: "{RESTART}"'


def test_all_uncited_claims_reported_in_order() -> None:
    draft = entry(
        rationale=Section(claims=(uncited("Replicas hold stale locks"),)),
        trigger=Section(claims=(uncited("Batch exits with code 3"),)),
    )
    with pytest.raises(UncitedClaimError) as excinfo:
        validate_citations(draft)
    assert [(claim.section, claim.index) for claim in excinfo.value.claims] == [
        ("trigger", 0),
        ("rationale", 0),
    ]
    message = str(excinfo.value)
    assert "trigger[0]" in message
    assert "rationale[0]" in message
    assert message.index("trigger[0]") < message.index("rationale[0]")


@pytest.mark.parametrize("name", SECTION_NAMES)
def test_every_section_is_checked(name: SectionName) -> None:
    draft = entry(**{name: Section(claims=(uncited("Unsupported statement"),))})
    assert find_uncited_claims(draft) == (
        UncitedClaim(section=name, index=0, text="Unsupported statement"),
    )


def test_open_section_without_claims_passes() -> None:
    validate_citations(entry(rollback=Section(open_question="Expert does not remember")))


def test_draft_may_hold_uncited_claims() -> None:
    draft = entry(steps=Section(claims=(uncited(RESTART),)))
    assert draft.steps.claims[0].citations == ()


def test_rejected_entry_may_hold_uncited_claims() -> None:
    rejected = entry(status="rejected", steps=Section(claims=(uncited(RESTART),)))
    assert rejected.status.value == "rejected"


def test_approved_entry_with_uncited_claim_cannot_be_constructed() -> None:
    payload = {
        "id": "RB-001",
        "topic_id": "topic-1",
        "title": "Recover billing",
        "status": "approved",
        "approved_by": "nadia.ferrand",
        "approved_at": "2026-11-02T09:30:00Z",
        "steps": {"claims": [{"text": RESTART, "citations": []}]},
    }
    with pytest.raises(ValidationError) as excinfo:
        RunbookEntry.model_validate(payload)
    assert "steps[0]" in str(excinfo.value)
    assert RESTART in str(excinfo.value)


def test_approved_entry_with_every_claim_cited_is_constructed() -> None:
    approved = entry(status="approved", steps=Section(claims=(cited(RESTART),)), **APPROVAL)
    assert approved.status.value == "approved"


def test_approving_a_copy_is_also_checked() -> None:
    draft = entry(steps=Section(claims=(uncited(RESTART),)))
    payload = draft.model_dump() | {"status": "approved", **APPROVAL}
    with pytest.raises(ValidationError, match="uncited claim"):
        RunbookEntry.model_validate(payload)


def test_model_copy_cannot_approve_an_uncited_entry() -> None:
    draft = entry(steps=Section(claims=(uncited(RESTART),)))
    with pytest.raises(ValidationError, match="uncited claim"):
        draft.model_copy(update={"status": "approved", **APPROVAL})


def test_model_copy_with_valid_update_still_works() -> None:
    draft = entry(steps=Section(claims=(cited(RESTART),)))
    approved = draft.model_copy(update={"status": "approved", **APPROVAL})
    assert approved.status.value == "approved"
    assert approved.steps == draft.steps
