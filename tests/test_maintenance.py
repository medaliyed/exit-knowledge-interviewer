"""F21: successor authority, attributed sources, freshness and scoped reader feedback."""

from typing import Any
from uuid import UUID

import pytest

from eki import access, maintenance
from eki.auth import Principal, acting_as
from eki.errors import ConflictError, NotFoundError
from eki.runbook.models import InterviewTurnCitation
from eki.runbook.review import SectionEdit
from eki.runbook.service import RunbookService
from tests.conftest import Scratch
from tests.search_support import ADMIN, OWNER, search_world

pytestmark = [pytest.mark.db, pytest.mark.anyio]
SUCCESSOR = Principal("samir", "Samir")


def cas(books: RunbookService, entry: UUID) -> dict[str, Any]:
    return {
        "expected_version": books.get(entry).version,
        "expected_maintenance_revision": books.metadata(entry)["maintenance_revision"],
    }


async def handover(scratch: Scratch) -> tuple[UUID, UUID]:
    world = await search_world(scratch)
    with scratch.connect() as conn:
        maintenance.transfer(
            conn,
            world.run_id,
            SUCCESSOR.subject,
            ADMIN,
            expected_owner=OWNER.subject,
            expected_revision=1,
            reason="Expert departure",
            start=True,
            identities=(SUCCESSOR,),
        )
    return world.run_id, world.billing


async def test_successor_edits_and_reapproves_with_attributed_sources(scratch: Scratch) -> None:
    run, entry = await handover(scratch)
    books = RunbookService(scratch.dsn)
    with acting_as(SUCCESSOR):
        changed = books.edit(
            entry,
            SUCCESSOR.name,
            {"steps": SectionEdit(claims=("Restart twice.",))},
            **cas(books, entry),
        )
        approved = books.approve(entry, SUCCESSOR.name, **cas(books, entry))
    assert approved.entry.approved_by == SUCCESSOR.name
    citation = changed.entry.steps.claims[0].citations[0]
    assert isinstance(citation, InterviewTurnCitation)
    with scratch.connect() as conn:
        turn = conn.execute(
            "SELECT speaker,author_subject,author_name FROM turns WHERE session_id=%s"
            " AND ordinal=%s",
            (changed.session_id, citation.turn_index),
        ).fetchone()
        assert turn == ("maintainer", "samir", "Samir")
        assert conn.execute(
            "SELECT expert_name FROM ingest_runs WHERE id=%s", (run,)
        ).fetchone() == (OWNER.name,)
    for who in (OWNER, ADMIN, Principal("impostor", SUCCESSOR.name)):
        with acting_as(who), pytest.raises(NotFoundError):
            books.edit(entry, who.name, {"steps": SectionEdit(claims=("Unauthorized.",))})
    with pytest.raises(NotFoundError):
        books.edit(entry, SUCCESSOR.name, {"steps": SectionEdit(claims=("Name only.",))})


async def test_transfer_is_compare_and_swap_and_does_not_rewrite_entry(scratch: Scratch) -> None:
    run, entry = await handover(scratch)
    before = RunbookService(scratch.dsn).get(entry)
    with scratch.connect() as conn:
        with pytest.raises(ConflictError):
            maintenance.transfer(
                conn,
                run,
                OWNER.subject,
                ADMIN,
                expected_owner=OWNER.subject,
                expected_revision=1,
                reason="stale",
                identities=(OWNER,),
            )
        assert access.readers(conn, run, ADMIN)["owner"] == SUCCESSOR.subject
    assert RunbookService(scratch.dsn).get(entry) == before


async def test_capture_cannot_resume_after_handover(scratch: Scratch) -> None:
    from tests.interview_support import make_service

    run, entry = await handover(scratch)
    service = make_service(scratch.dsn)
    with pytest.raises(ConflictError, match="closed"):
        await service.draft_plan(run)
    with pytest.raises(ConflictError, match="closed"):
        await service.post(RunbookService(scratch.dsn).get(entry).session_id, text="Not the expert")


async def test_second_successor_can_review_previous_maintainer_evidence(scratch: Scratch) -> None:
    run, entry = await handover(scratch)
    books = RunbookService(scratch.dsn)
    with acting_as(SUCCESSOR):
        books.edit(
            entry,
            SUCCESSOR.name,
            {"steps": SectionEdit(claims=("Restart twice.",))},
            **cas(books, entry),
        )
    next_owner = Principal("lee", "Lee")
    with scratch.connect() as conn:
        maintenance.transfer(
            conn,
            run,
            next_owner.subject,
            ADMIN,
            expected_owner=SUCCESSOR.subject,
            expected_revision=2,
            reason="Rotation",
            identities=(next_owner,),
        )
    with acting_as(next_owner):
        assert books.approve(entry, next_owner.name, **cas(books, entry)).entry.approved_by == "Lee"


async def test_review_renews_metadata_without_changing_content_or_approval(
    scratch: Scratch,
) -> None:
    from datetime import UTC, datetime, timedelta

    _run, entry = await handover(scratch)
    books = RunbookService(scratch.dsn)
    before = books.get(entry)
    meta = books.metadata(entry)
    with acting_as(SUCCESSOR):
        books.review(
            entry,
            SUCCESSOR.name,
            expected_version=before.version,
            expected_maintenance_revision=meta["maintenance_revision"],
        )
    after = books.get(entry)
    assert after == before
    assert books.metadata(entry)["last_reviewed_by_subject"] == SUCCESSOR.subject
    now = datetime.now(UTC)
    assert books.metadata(entry)["review_by"] == (now.date() + timedelta(days=90)).isoformat()
    with acting_as(SUCCESSOR), pytest.raises(ConflictError):
        books.review(
            entry,
            SUCCESSOR.name,
            expected_version=before.version,
            expected_maintenance_revision=meta["maintenance_revision"],
        )


async def test_overdue_search_export_and_answer_policy(scratch: Scratch) -> None:
    from datetime import UTC, datetime, timedelta

    from eki.runbook.export import ExportService
    from eki.search.answers import AnswerService
    from eki.search.service import SearchService
    from tests.search_support import READER

    world = await search_world(scratch)
    today = datetime.now(UTC).date()
    with scratch.connect() as conn:
        conn.execute(
            "UPDATE runbook_entries SET review_by=%s WHERE id=%s",
            (today - timedelta(days=1), world.billing),
        )
    hits = SearchService(scratch.dsn).search(READER, "month-end reconciliation")
    assert any(
        h.entry.id == str(world.billing) and h.public()["freshness"] == "overdue" for h in hits
    )
    assert "Review overdue" in ExportService(scratch.dsn).entry(READER, world.billing)
    answer = await AnswerService(SearchService(scratch.dsn)).ask(
        READER, "How do I run month-end reconciliation?"
    )
    assert all(e.entry_id != str(world.billing) for e in answer.evidence)
