"""F22 9c: entry proposals, source lineage, correction propagation and G7 gap resolution."""

import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest

from eki.followup import proposals
from eki.followup.gaps import GapService, open_section_gap_id
from eki.followup.sessions import FollowUpService, finish, outcome
from eki.interview import store
from eki.runbook.review import SectionEdit
from eki.runbook.service import RunbookService
from eki.search.answers import AnswerService
from eki.search.service import SearchService
from tests.conftest import Scratch
from tests.followup_support import (
    OPEN_REASON,
    answer_follow_up,
    answered_follow_up,
    gap_world,
    triaged_world,
)
from tests.interview_support import TOPIC_IDS
from tests.search_support import OWNER, READER

pytestmark = [pytest.mark.db, pytest.mark.anyio]


def queue(scratch: Scratch, run_id: UUID, filter: str = "open") -> dict[str, dict[str, Any]]:
    listed = GapService(scratch.dsn).queue(run_id, OWNER, filter=filter)["gaps"]
    return {g["id"]: g for g in listed}


# --- G7 / A1 / A10: the queue keeps answered work visible ---


async def test_queue_keeps_answered_open_section_after_section_is_filled(
    scratch: Scratch,
) -> None:
    done = await answered_follow_up(scratch)
    world = done.world
    before = queue(scratch, world.run_id)[done.rollback_gap]
    assert before["status"] == "awaiting_review" and before["live"] is True
    books = RunbookService(scratch.dsn)
    books.edit(world.billing, OWNER.name, {"rollback": SectionEdit(claims=("Restore export.",))})
    after = queue(scratch, world.run_id)
    assert done.rollback_gap in after, "answered work must not vanish when its section fills"
    gap = after[done.rollback_gap]
    assert gap["status"] == "awaiting_review" and gap["live"] is False
    assert gap["open_question"] is None and gap["entry_title"]
    assert gap["entry_status"] == "draft" and gap["section"] == "rollback"
    assert [a["outcome"] for a in gap["attempts"]] == ["answered"]


async def test_non_live_pending_open_section_stays_hidden(scratch: Scratch) -> None:
    world = await gap_world(scratch)
    service = GapService(scratch.dsn)
    gap_id = str(open_section_gap_id(world.billing, "rollback"))
    deferred = service.triage(
        world.run_id, gap_id, OWNER, action="defer", expected_revision=0, reason="Later"
    )
    service.triage(
        world.run_id, gap_id, OWNER, action="reopen", expected_revision=deferred["revision"]
    )
    RunbookService(scratch.dsn).edit(
        world.billing, OWNER.name, {"rollback": SectionEdit(claims=("Restore export.",))}
    )
    assert queue(scratch, world.run_id, "all") == {}


async def test_reopened_section_after_resolution_is_triageable_again(scratch: Scratch) -> None:
    world = await gap_world(scratch)
    service = GapService(scratch.dsn)
    gap_id = str(open_section_gap_id(world.billing, "rollback"))
    books = RunbookService(scratch.dsn)
    books.edit(world.billing, OWNER.name, {"rollback": SectionEdit(claims=("Restore export.",))})
    stored = books.approve(world.billing, OWNER.name)
    with scratch.connect() as conn:
        conn.execute(
            "INSERT INTO follow_up_gaps(id,run_id,kind,entry_id,section,created_at,status,"
            "revision,resolved_entry_id,resolved_version,resolved_at,resolved_by_subject) "
            "VALUES (%s,%s,'open_section',%s,'rollback',now(),'resolved',3,%s,%s,now(),%s)",
            (UUID(gap_id), world.run_id, world.billing, world.billing, stored.version, "nadia"),
        )
    resolved = queue(scratch, world.run_id, "all")[gap_id]
    assert resolved["status"] == "resolved" and resolved["live"] is False
    assert queue(scratch, world.run_id).get(gap_id) is None, "resolved is not open"
    books.edit(world.billing, OWNER.name, {"rollback": SectionEdit(open_question=OPEN_REASON)})
    reopened = queue(scratch, world.run_id)[gap_id]
    assert reopened["status"] == "pending" and reopened["reopened"] is True
    assert reopened["live"] is True and reopened["resolved_version"] == stored.version
    assert reopened["revision"] == 3
    triaged = service.triage(
        world.run_id, gap_id, OWNER, action="defer", expected_revision=3, reason="Not now"
    )
    assert triaged["status"] == "deferred" and triaged["revision"] == 4
    assert triaged["resolved_version"] is None and triaged["resolved_at"] is None


# --- P1 / A7: finishing a follow-up creates one private proposal ---


def proposal_rows(scratch: Scratch, session_id: UUID) -> list[tuple[Any, ...]]:
    with scratch.connect() as conn:
        return conn.execute(
            "SELECT id, entry_id, topic_id, base_version, status, revision, patch, gap_ids "
            "FROM follow_up_proposals WHERE session_id=%s ORDER BY id",
            (session_id,),
        ).fetchall()


def audit_details(scratch: Scratch, operation: str) -> list[dict[str, Any]]:
    with scratch.connect() as conn:
        return [
            r[0]
            for r in conn.execute(
                "SELECT details FROM audit_events WHERE details->>'operation'=%s ORDER BY id",
                (operation,),
            ).fetchall()
        ]


def index_count(scratch: Scratch, entry_id: UUID) -> int:
    with scratch.connect() as conn:
        row = conn.execute(
            "SELECT count(*) FROM runbook_index WHERE entry_id=%s", (entry_id,)
        ).fetchone()
    assert row is not None
    return int(row[0])


async def test_finish_creates_one_private_proposal_and_leaves_entry_untouched(
    scratch: Scratch,
) -> None:
    world, rollback_gap, request_gap = await triaged_world(scratch)
    search = SearchService(scratch.dsn)
    books = RunbookService(scratch.dsn)
    assert search.search(READER, "Noted contacts billing"), "index the approved entry first"
    before = books.get(world.billing)
    indexed = index_count(scratch, world.billing)
    decisions = books.history(world.billing)
    assert indexed > 0
    done = await answer_follow_up(scratch, world, rollback_gap, request_gap)
    assert books.get(world.billing) == before, "content, version and status are unchanged"
    assert index_count(scratch, world.billing) == indexed
    assert books.history(world.billing) == decisions
    hits = search.search(READER, "Restore the pre-run export rollback")
    assert all("pre-run" not in h.snippet for h in hits)
    assert all(h.version == before.version for h in hits if h.entry.id == str(world.billing))
    answer = await AnswerService(search).ask(READER, "How do I roll back billing?")
    assert "pre-run" not in answer.model_dump_json()
    ((pid, entry_id, topic_id, base_version, status, revision, patch, gap_ids),) = proposal_rows(
        scratch, done.session_id
    )
    assert pid == proposals.proposal_id(done.session_id, TOPIC_IDS[0])
    assert (entry_id, topic_id) == (world.billing, TOPIC_IDS[0])
    assert (base_version, status, revision) == (before.version, "pending", 1)
    assert set(patch) == {"rollback", "contacts"}
    assert patch["rollback"]["clears_open"] is True
    assert sorted(str(g) for g in gap_ids) == sorted([rollback_gap, request_gap])
    (event,) = audit_details(scratch, "follow_up_proposed")
    assert event["target_id"] == str(pid) and event["version"] == before.version
    assert event["count"] == 2
    assert "pre-run" not in json.dumps(event) and "CFO" not in json.dumps(event)


# --- P1 / A8: replays never duplicate a proposal ---


async def test_finish_replays_create_no_duplicates(scratch: Scratch) -> None:
    done = await answered_follow_up(scratch)
    books = RunbookService(scratch.dsn)
    decisions = books.history(done.world.billing)
    with scratch.connect() as conn:
        finish(conn, done.session_id)
        session = store.load_session(conn, done.session_id)
        assert session is not None
        outcomes = {
            q.gap_id: outcome(conn, done.session_id, q.gap_id)[0]
            for t in (store.load_plan(conn, session.plan_id) or pytest.fail()).topics
            for q in t.questions
            if q.gap_id
        }
        with conn.transaction():
            assert proposals.ensure(conn, done.session_id, outcomes, now=datetime.now(UTC)) == ()
    assert FollowUpService(scratch.dsn).stop(done.session_id, OWNER)["status"] == "finished"
    books.synthesise(done.session_id)
    books.synthesise(done.session_id)
    assert len(proposal_rows(scratch, done.session_id)) == 1
    assert len(audit_details(scratch, "follow_up_proposed")) == 1
    assert books.list(done.session_id) == (), "a mapped topic never gets a second entry"
    assert books.history(done.world.billing) == decisions, "no decision is recorded"
    with scratch.connect() as conn:
        # A session finished before migration 0018 has no proposal; explicit synthesis
        # in capture mode backfills it once.
        conn.execute("DELETE FROM follow_up_proposals WHERE session_id=%s", (done.session_id,))
    books.synthesise(done.session_id)
    books.synthesise(done.session_id)
    assert len(proposal_rows(scratch, done.session_id)) == 1
    assert len(audit_details(scratch, "follow_up_proposed")) == 2
