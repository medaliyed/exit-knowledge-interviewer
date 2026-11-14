"""F10: decisions, evidence checks and concurrent review in Postgres."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from uuid import UUID, uuid4

import pytest

from eki.errors import ConflictError, NotFoundError
from eki.runbook.service import RunbookService
from tests.conftest import Scratch
from tests.test_workflow_synthesis import finished

pytestmark = [pytest.mark.db, pytest.mark.anyio]


async def test_approve_reject_and_synthesis_preserves_decisions(scratch: Scratch) -> None:
    session = await finished(scratch)
    service = RunbookService(scratch.dsn)
    first, second, third = service.list(session)
    with pytest.raises(ValueError):
        service.approve(UUID(first.entry.id), "Wrong person")
    approved = service.approve(UUID(first.entry.id), "Nadia Ferrand")
    rejected = service.reject(UUID(second.entry.id), "Nadia Ferrand", "Not useful")
    rebuilt = service.synthesise(session)
    assert rebuilt[:2] == (approved, rejected)
    assert rebuilt[2].entry == third.entry
    assert service.history(UUID(first.entry.id))[0].action == "approve"
    decision = service.history(UUID(second.entry.id))[0]
    assert (decision.action, decision.reason, decision.who) == (
        "reject",
        "Not useful",
        "Nadia Ferrand",
    )
    assert rejected.rejection_reason == "Not useful"
    with pytest.raises(ConflictError):
        service.approve(UUID(second.entry.id), "Nadia Ferrand")
    with pytest.raises(NotFoundError):
        service.approve(uuid4(), "Nadia Ferrand")


async def test_decisions_are_serialised(scratch: Scratch) -> None:
    session = await finished(scratch)
    service = RunbookService(scratch.dsn)
    entry = UUID(service.list(session)[0].entry.id)

    def decide() -> str:
        try:
            service.approve(entry, "Nadia Ferrand")
            return "approved"
        except ConflictError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = await asyncio.gather(
            *(asyncio.wrap_future(pool.submit(decide)) for _ in range(2))
        )
    assert sorted(results) == ["approved", "conflict"]
    assert len(service.history(entry)) == 1


@pytest.mark.parametrize(
    "source", ["missing", "interviewer", "other_topic", "confidential", "other_session"]
)
async def test_approval_checks_source_turns(scratch: Scratch, source: str) -> None:
    from eki.ingest.redaction import RedactedText
    from eki.interview import store as interviews
    from eki.runbook import store
    from eki.runbook.models import Claim, InterviewTurnCitation, Section

    session = await finished(scratch)
    service = RunbookService(scratch.dsn)
    previous = service.list(session)[0]
    ordinal = {"missing": 999, "interviewer": 1, "other_topic": 4}.get(source, 2)
    with scratch.connect() as conn:
        if source == "confidential":
            ordinal = interviews.add_expert_turn(
                conn,
                session,
                previous.entry.topic_id,
                "answer",
                RedactedText("private"),
                confidential=True,
            )
        citation = InterviewTurnCitation(
            session_id=str(uuid4() if source == "other_session" else session), turn_index=ordinal
        )
        bad = previous.entry.model_copy(
            update={"steps": Section(claims=(Claim(text="Bad evidence", citations=(citation,)),))}
        )
        store.save_entry(conn, session, bad)
    with pytest.raises(ValueError):
        service.approve(UUID(previous.entry.id), "Nadia Ferrand")
    assert service.get(UUID(previous.entry.id)).entry.status == "draft"
    assert service.history(UUID(previous.entry.id)) == ()
