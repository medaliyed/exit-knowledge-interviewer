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


async def test_review_and_reader_flags_preserve_existing_embeddings(scratch: Scratch) -> None:
    from uuid import uuid4

    from eki.feedback import FeedbackService
    from eki.search.service import SearchService
    from tests.search_support import READER

    _, entry = await handover(scratch)
    SearchService(scratch.dsn).search(READER, "month-end reconciliation")
    with scratch.connect() as conn:
        before = conn.execute(
            "SELECT section,ordinal,version,model,embedding::text FROM runbook_index "
            "WHERE entry_id=%s ORDER BY section,ordinal",
            (entry,),
        ).fetchall()
    assert before
    books = RunbookService(scratch.dsn)
    with acting_as(SUCCESSOR):
        books.review(entry, SUCCESSOR.name, **cas(books, entry))
    FeedbackService(scratch.dsn).create(
        entry,
        READER,
        request_id=uuid4(),
        expected_version=books.get(entry).version,
        category="unclear",
        description="Please clarify",
    )
    with scratch.connect() as conn:
        assert (
            conn.execute(
                "SELECT section,ordinal,version,model,embedding::text FROM runbook_index "
                "WHERE entry_id=%s ORDER BY section,ordinal",
                (entry,),
            ).fetchall()
            == before
        )


async def test_transfer_racing_old_owner_write_refuses_the_stale_writer(
    scratch: Scratch, monkeypatch: pytest.MonkeyPatch
) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event, current_thread

    run, entry = await handover(scratch)
    books = RunbookService(scratch.dsn)
    before = books.get(entry)
    args = cas(books, entry)
    reached = Event()
    original = maintenance.lock_run

    def locking(conn: Any, run_id: UUID) -> tuple[Any, ...]:
        if current_thread().name.startswith("old-owner"):
            reached.set()
        return original(conn, run_id)

    monkeypatch.setattr(maintenance, "lock_run", locking)

    def stale_write() -> None:
        with acting_as(SUCCESSOR):
            books.edit(
                entry, SUCCESSOR.name, {"steps": SectionEdit(claims=("Stale change.",))}, **args
            )

    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="old-owner") as pool:
        with scratch.connect() as conn, conn.transaction():
            original(conn, run)
            future = pool.submit(stale_write)
            assert reached.wait(timeout=5)
            maintenance.transfer(
                conn,
                run,
                "lee",
                ADMIN,
                expected_owner=SUCCESSOR.subject,
                expected_revision=2,
                reason="Rotation",
                identities=(Principal("lee", "Lee"),),
            )
        with pytest.raises(NotFoundError):
            future.result(timeout=10)
    assert books.get(entry) == before


async def test_q_and_a_suppresses_evidence_that_becomes_overdue_during_model_latency(
    scratch: Scratch,
) -> None:
    from datetime import UTC, datetime, timedelta

    from eki.llm import Reply
    from eki.search.answers import AnswerService
    from eki.search.service import SearchService
    from tests.fakes import ScriptedModel
    from tests.search_support import READER

    world = await search_world(scratch)
    clock = [datetime.now(UTC).replace(hour=23, minute=59, second=59)]
    with scratch.connect() as conn:
        conn.execute(
            "UPDATE runbook_entries SET review_by=%s WHERE id=%s", (clock[0].date(), world.billing)
        )

    def answer(messages: Any, tools: Any) -> Reply:
        clock[0] += timedelta(seconds=2)
        return Reply(text='{"evidence_ids":["E1"],"abstain":false}')

    result = await AnswerService(
        SearchService(scratch.dsn, clock=lambda: clock[0]), ScriptedModel(answer)
    ).ask(READER, "How do I run month-end reconciliation?")
    assert result.status == "insufficient_evidence" and not result.evidence


async def test_departed_expert_can_be_disabled_and_disabled_destinations_are_refused(
    scratch: Scratch,
) -> None:
    from uuid import uuid4

    world = await search_world(scratch)
    with scratch.connect() as conn:
        conn.execute(
            "INSERT INTO accounts(id,subject,email,name,disabled,created_at) "
            "VALUES(%s,%s,%s,%s,true,now())",
            (uuid4(), OWNER.subject, "nadia.ferrand@example.com", OWNER.name),
        )
        maintenance.transfer(
            conn,
            world.run_id,
            SUCCESSOR.subject,
            ADMIN,
            expected_owner=OWNER.subject,
            expected_revision=1,
            reason="Expert left",
            start=True,
            identities=(SUCCESSOR,),
        )
        conn.execute(
            "INSERT INTO accounts(id,subject,email,name,disabled,created_at) "
            "VALUES(%s,'blocked','blocked@example.com','Blocked',true,now())",
            (uuid4(),),
        )
        with pytest.raises(ValueError, match="active"):
            maintenance.transfer(
                conn,
                world.run_id,
                "blocked",
                ADMIN,
                expected_owner=SUCCESSOR.subject,
                expected_revision=2,
                reason="Invalid target",
                identities=(Principal("blocked", "Blocked"),),
            )
    books = RunbookService(scratch.dsn)
    with acting_as(SUCCESSOR):
        books.edit(
            world.billing,
            SUCCESSOR.name,
            {"steps": SectionEdit(claims=("Restart twice.",))},
            **cas(books, world.billing),
        )
        assert (
            books.approve(
                world.billing, SUCCESSOR.name, **cas(books, world.billing)
            ).entry.approved_by
            == SUCCESSOR.name
        )


async def test_pending_accepted_answer_blocks_handover(scratch: Scratch) -> None:
    world = await search_world(scratch)
    with scratch.connect() as conn:
        conn.execute(
            "UPDATE message_receipts SET status='needs_retry',result=NULL WHERE session_id=%s",
            (world.session_id,),
        )
        with pytest.raises(ConflictError, match="recover"):
            maintenance.transfer(
                conn,
                world.run_id,
                SUCCESSOR.subject,
                ADMIN,
                expected_owner=OWNER.subject,
                expected_revision=1,
                reason="Expert left",
                start=True,
                identities=(SUCCESSOR,),
            )
        assert maintenance.state(conn, world.run_id, ADMIN)["maintenance_started_at"] is None


def test_legacy_migration_backfills_actual_approval_without_inventing_identity(
    empty_conn: Any, tmp_path: Any
) -> None:
    from datetime import UTC, datetime, timedelta
    from uuid import uuid4

    from psycopg.types.json import Jsonb

    from eki.db import MIGRATIONS_DIR, migrate
    from eki.runbook.models import SECTION_NAMES, RunbookEntry, Section

    legacy_dir = tmp_path / "legacy"
    legacy_dir.mkdir()
    for path in MIGRATIONS_DIR.glob("*.sql"):
        if path.name < "0013":
            (legacy_dir / path.name).write_text(path.read_text())
    migrate(empty_conn, legacy_dir)
    run, plan, session, entry = (uuid4() for _ in range(4))
    at = datetime(2020, 1, 1, 23, 59, tzinfo=UTC)
    content = RunbookEntry(
        id=str(entry),
        topic_id="legacy",
        title="Legacy",
        status="approved",
        approved_by=OWNER.name,
        approved_at=at,
        **{s: Section(open_question="Unknown") for s in SECTION_NAMES},
    ).model_dump(mode="json")
    empty_conn.execute(
        "INSERT INTO ingest_runs(id,expert_name,expert_email,since,until,status) "
        "VALUES(%s,%s,'nadia@example.com',now(),now(),'completed')",
        (run, OWNER.name),
    )
    empty_conn.execute(
        "INSERT INTO interview_plans(id,run_id,status) VALUES(%s,%s,'draft')", (plan, run)
    )
    empty_conn.execute(
        "INSERT INTO sessions(id,plan_id,status) VALUES(%s,%s,'finished')", (session, plan)
    )
    empty_conn.execute(
        "INSERT INTO runbook_entries(id,session_id,topic_id,title,status,content,"
        "approved_by,approved_at,version) "
        "VALUES(%s,%s,'legacy','Legacy','approved',%s,%s,%s,7)",
        (entry, session, Jsonb(content), OWNER.name, at),
    )
    empty_conn.commit()
    migrate(empty_conn)
    row = empty_conn.execute(
        "SELECT review_by,last_reviewed_at,last_reviewed_by_subject,last_reviewed"
        "_by_name,reviewed_version,status,content FROM runbook_entries WHERE "
        "id=%s",
        (entry,),
    ).fetchone()
    assert row == (at.date() + timedelta(days=90), at, None, OWNER.name, 7, "approved", content)
    assert empty_conn.execute(
        "SELECT maintenance_started_at,maintenance_revision FROM ingest_runs WHERE id=%s", (run,)
    ).fetchone() == (None, 0)
