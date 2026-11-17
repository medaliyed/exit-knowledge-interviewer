"""F22 9c: entry proposals, source lineage, correction propagation and G7 gap resolution."""

import json
from datetime import UTC, datetime
from threading import Barrier, Thread
from typing import Any
from uuid import UUID, uuid4

import pytest
from psycopg.conninfo import make_conninfo

from eki import access, accounts
from eki.auth import acting_as
from eki.errors import ConflictError, NotFoundError
from eki.followup import proposals
from eki.followup.gaps import GapService, open_section_gap_id
from eki.followup.sessions import FollowUpService, finish, outcome
from eki.ingest.redaction import RedactedText
from eki.interview import store
from eki.interview.models import Turn
from eki.runbook import store as entry_store
from eki.runbook.export import ExportService
from eki.runbook.faithfulness import check_entry
from eki.runbook.models import Claim, InterviewTurnCitation, RunbookEntry, Section
from eki.runbook.review import SectionEdit
from eki.runbook.service import RunbookService, validate_sources
from eki.search.answers import AnswerService
from eki.search.service import SearchService
from tests.conftest import Scratch
from tests.followup_support import (
    CONTACT_ANSWERS,
    OPEN_REASON,
    ROLLBACK_ANSWER,
    AnsweredFollowUp,
    answer_follow_up,
    answered_follow_up,
    gap_world,
    triaged_world,
)
from tests.interview_support import TOPIC_IDS, make_service, say
from tests.search_support import OWNER, READER

pytestmark = [pytest.mark.db, pytest.mark.anyio]
ORIGIN = "https://testserver"
PASSWORD = "correct horse battery staple 42"


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


# --- P5 / A9: claims may cite only explicitly linked follow-up turns ---


def turn_of(scratch: Scratch, session_id: UUID, text: str, kind: str = "answer") -> int:
    with scratch.connect() as conn:
        turns = store.load_turns(conn, session_id)
    return next(t.ordinal for t in turns if t.kind == kind and t.text == text)


def citing(entry: RunbookEntry, session_id: str, turn: int) -> RunbookEntry:
    claim = Claim(
        text="Restore the pre-run export.",
        citations=(InterviewTurnCitation(session_id=session_id, turn_index=turn),),
    )
    return entry.model_copy(update={"rollback": Section(claims=(claim,))})


async def test_sources_accept_only_explicitly_linked_follow_up_turns(scratch: Scratch) -> None:
    done = await answered_follow_up(scratch)
    world, fid = done.world, done.session_id
    books = RunbookService(scratch.dsn)
    billing = books.get(world.billing)
    home = billing.session_id
    answer = turn_of(scratch, fid, ROLLBACK_ANSWER)
    with scratch.connect() as conn:
        validate_sources(conn, home, billing.entry)
        validate_sources(conn, home, citing(billing.entry, str(fid), answer))
        validate_sources(conn, home, citing(billing.entry, str(fid).upper(), answer))
        topic = billing.entry.topic_id
        secret = store.add_expert_turn(
            conn, fid, topic, "answer", RedactedText("Secret."), confidential=True
        )
        skipped = store.add_expert_turn(conn, fid, topic, "skip", RedactedText("(skipped)"))
        edited = store.add_expert_turn(conn, fid, topic, "edit", RedactedText("rollback: x"))
        other = store.add_expert_turn(conn, fid, TOPIC_IDS[1], "answer", RedactedText("Other."))
        question = next(t.ordinal for t in store.load_turns(conn, fid) if t.kind == "question")
        rejected = {
            "forged ordinal": (str(fid), 999),
            "confidential": (str(fid), secret),
            "skip": (str(fid), skipped),
            "edit turn in a linked session": (str(fid), edited),
            "another topic": (str(fid), other),
            "interviewer": (str(fid), question),
            "malformed session": ("not-a-session", answer),
        }
        for why, (session, turn) in rejected.items():
            with pytest.raises(ValueError):
                validate_sources(conn, home, citing(billing.entry, session, turn))
                pytest.fail(why)
        corrected = store.add_expert_turn(
            conn, fid, topic, "correction", RedactedText("Restore it twice."), corrects=answer
        )
        with pytest.raises(ValueError, match="corrected"):
            validate_sources(conn, home, citing(billing.entry, str(fid), answer))
        validate_sources(conn, home, citing(billing.entry, str(fid), corrected))
        # A follow-up in the same run that has no proposal for this entry is not lineage.
        conn.execute(
            "UPDATE follow_up_proposals SET entry_id=%s WHERE session_id=%s",
            (world.ledger, fid),
        )
        with pytest.raises(ValueError, match="linked follow-up"):
            validate_sources(conn, home, citing(billing.entry, str(fid), corrected))


# --- P3 / A7: explicit apply of selected sections ---


def gap_statuses(scratch: Scratch, run_id: UUID) -> dict[str, str]:
    return {g: v["status"] for g, v in queue(scratch, run_id, "all").items()}


async def test_apply_selected_sections_preserves_everything_else(scratch: Scratch) -> None:
    done = await answered_follow_up(scratch)
    world, fid = done.world, done.session_id
    books = RunbookService(scratch.dsn)
    pid = proposals.proposal_id(fid, TOPIC_IDS[0])
    assert SearchService(scratch.dsn).search(READER, "Noted contacts billing")
    assert index_count(scratch, world.billing) > 0
    base = books.get(world.billing)
    books.edit(world.billing, OWNER.name, {"steps": SectionEdit(claims=("Run it twice.",))})
    edited = books.get(world.billing)
    with acting_as(OWNER):
        (stale,) = books.proposals_for_session(fid)
        assert stale["stale"] is True and stale["base_version"] == base.version
        with pytest.raises(ConflictError, match="refresh"):
            books.apply_proposal(
                pid,
                OWNER.name,
                {"rollback": "merge"},
                expected_revision=1,
                expected_version=edited.version,
            )
        refreshed = books.refresh_proposal(
            pid, OWNER.name, expected_revision=1, expected_version=edited.version
        )
    assert refreshed["revision"] == 2 and refreshed["base_version"] == edited.version
    assert refreshed["stale"] is False
    review_by = books.metadata(world.billing)["review_by"]
    with acting_as(OWNER):
        stored, replayed = books.apply_proposal(
            pid,
            OWNER.name,
            {"rollback": "merge"},
            expected_revision=2,
            expected_version=edited.version,
        )
    assert replayed is False
    assert stored.entry.id == str(world.billing) and stored.version == edited.version + 1
    assert stored.entry.status == "draft" and stored.edited is True
    assert stored.entry.approved_by is None and stored.entry.approved_at is None
    for name in ("trigger", "diagnosis", "steps", "contacts", "pitfalls", "rationale"):
        assert stored.entry.section(name) == edited.entry.section(name), name
    assert stored.entry.steps.claims[0].text == "Run it twice.", "the hand edit is kept"
    (claim,) = stored.entry.rollback.claims
    assert claim.text == ROLLBACK_ANSWER and stored.entry.rollback.open_question is None
    assert claim.citations[0].session_id == str(fid)  # type: ignore[union-attr]
    assert index_count(scratch, world.billing) == 0
    with scratch.connect() as conn, pytest.raises(NotFoundError):
        access.approved_entry(conn, world.billing, READER)
    statuses = gap_statuses(scratch, world.run_id)
    assert statuses[done.request_gap] == "pending", "unapplied contacts work returns to triage"
    assert statuses[done.rollback_gap] == "awaiting_review"
    actions = [d.action for d in books.history(world.billing)]
    assert actions.count("follow_up_apply") == 1 and actions[-1] == "follow_up_apply"
    assert books.metadata(world.billing)["review_by"] == review_by
    ((_, _, _, _, status, revision, _, _),) = proposal_rows(scratch, fid)
    assert (status, revision) == ("applied", 3)


async def test_replace_is_explicit_and_section_scoped(scratch: Scratch) -> None:
    done = await answered_follow_up(scratch)
    books = RunbookService(scratch.dsn)
    pid = proposals.proposal_id(done.session_id, TOPIC_IDS[0])
    base = books.get(done.world.billing)
    bad_choices: list[Any] = [{}, {"steps": "merge"}, {"contacts": "overwrite"}]
    for bad in bad_choices:
        with acting_as(OWNER), pytest.raises(ValueError):
            books.apply_proposal(
                pid, OWNER.name, bad, expected_revision=1, expected_version=base.version
            )
    with acting_as(OWNER):
        stored, _ = books.apply_proposal(
            pid,
            OWNER.name,
            {"contacts": "replace", "rollback": "merge"},
            expected_revision=1,
            expected_version=base.version,
        )
    assert [c.text for c in stored.entry.contacts.claims] == list(CONTACT_ANSWERS)
    assert stored.entry.trigger == base.entry.trigger
    assert stored.entry.pitfalls == base.entry.pitfalls, "the confidential marker stays"
    assert gap_statuses(scratch, done.world.run_id)[done.request_gap] == "awaiting_review"


async def test_http_proposal_routes(scratch: Scratch) -> None:
    from fastapi.testclient import TestClient

    from eki.api.app import create_app
    from eki.config import Settings

    done = await answered_follow_up(scratch)
    world, fid = done.world, done.session_id
    pid = proposals.proposal_id(fid, TOPIC_IDS[0])
    owner_token = "proposal-owner-token-0000000000000000001"
    reader_token = "proposal-reader-token-000000000000000001"
    with scratch.connect() as conn:
        account = accounts.create_account(
            conn,
            email="nadia@example.com",
            name=OWNER.name,
            subject=OWNER.subject,
            work_domains=frozenset({"example.com"}),
        )
        accounts.set_password(conn, account.id, PASSWORD, now=datetime.now(UTC))
    settings = Settings(
        _env_file=None,
        database_url=scratch.dsn,
        public_origin=ORIGIN,
        api_tokens=json.dumps(
            [
                {"token": owner_token, "subject": OWNER.subject, "name": OWNER.name},
                {"token": reader_token, "subject": READER.subject, "name": READER.name},
            ]
        ),
    )
    app = create_app(runbook_service=RunbookService(scratch.dsn), settings=settings)
    client = TestClient(app, base_url=ORIGIN)
    owner = {"Authorization": "Bearer " + owner_token}
    reader = {"Authorization": "Bearer " + reader_token}
    listing = f"/sessions/{fid}/follow-up-proposals"
    apply, refresh = f"/follow-up-proposals/{pid}/apply", f"/follow-up-proposals/{pid}/refresh"
    version = RunbookService(scratch.dsn).get(world.billing).version
    body = {
        "applied_by": OWNER.name,
        "sections": {"rollback": "merge"},
        "expected_revision": 1,
        "expected_version": version,
    }
    refresh_body = {"refreshed_by": OWNER.name, "expected_revision": 1, "expected_version": 1}
    for path, payload in ((apply, body), (refresh, refresh_body)):
        assert client.post(path, json=payload).status_code == 401
        assert client.post(path, headers=reader, json=payload).status_code == 404
    assert client.get(listing).status_code == 401
    assert client.get(listing, headers=reader).status_code == 404
    unknown = f"/follow-up-proposals/{uuid4()}/apply"
    assert client.post(unknown, headers=owner, json=body).status_code == 404
    shown = client.get(listing, headers=owner)
    assert shown.status_code == 200, shown.text
    (proposal,) = shown.json()["proposals"]
    assert proposal["id"] == str(pid) and proposal["status"] == "pending"
    assert proposal["can_apply"] is True and proposal["stale"] is False
    assert {s["section"] for s in proposal["sections"]} == {"rollback", "contacts"}
    rollback = next(s for s in proposal["sections"] if s["section"] == "rollback")
    assert rollback["clears_open"] is True and rollback["base"]["open_question"] == OPEN_REASON
    assert rollback["merged"]["open_question"] is None
    assert "Secret" not in shown.text and "confidential" not in json.dumps(proposal["sections"])
    for wrong in (
        {**body, "sections": {}},
        {**body, "sections": {"steps": "merge"}},
        {**body, "sections": {"rollback": "overwrite"}},
        {**body, "sections": {"rollback": ""}},
        {**body, "applied_by": "Someone Else"},
    ):
        assert client.post(apply, headers=owner, json=wrong).status_code == 422, wrong
    stale = {**body, "expected_version": version + 5}
    assert client.post(apply, headers=owner, json=stale).status_code == 409
    browser = TestClient(app, base_url=ORIGIN)
    signed_in = browser.post(
        "/auth/login",
        headers={"Origin": ORIGIN},
        json={"email": "nadia@example.com", "password": PASSWORD},
    )
    assert signed_in.status_code == 200, signed_in.text
    assert (
        browser.post(apply, headers={"Origin": "https://evil.test"}, json=body).status_code == 403
    )
    applied = client.post(apply, headers=owner, json=body)
    assert applied.status_code == 200, applied.text
    result = applied.json()
    assert result["replayed"] is False and result["proposal"]["status"] == "applied"
    assert result["entry"]["version"] == version + 1 and result["entry"]["status"] == "draft"
    assert result["entry"]["decisions"][-1]["action"] == "follow_up_apply"
    again = client.post(apply, headers=owner, json=body)
    assert again.status_code == 200 and again.json()["replayed"] is True
    other = {**body, "sections": {"contacts": "merge"}}
    assert client.post(apply, headers=owner, json=other).status_code == 409
    assert client.post(refresh, headers=owner, json=refresh_body).status_code == 409


# --- P3 / A8: conflicts preserve the proposal; refresh then apply succeeds ---


def proposal_state(scratch: Scratch, pid: UUID) -> tuple[Any, ...]:
    with scratch.connect() as conn:
        row = conn.execute(
            "SELECT status, revision, base_version, patch FROM follow_up_proposals WHERE id=%s",
            (pid,),
        ).fetchone()
    assert row is not None
    return tuple(row)


def versions(books: RunbookService, entry_id: UUID) -> tuple[int, int]:
    return books.get(entry_id).version, books.metadata(entry_id)["maintenance_revision"]


async def concurrent_change(
    scratch: Scratch, done: AnsweredFollowUp, books: RunbookService, change: str
) -> None:
    entry = done.world.billing
    with acting_as(OWNER):
        if change == "edit":
            books.edit(entry, OWNER.name, {"steps": SectionEdit(claims=("Changed step.",))})
        elif change == "reject":
            books.reject(entry, OWNER.name, "Outdated")
        elif change in ("approve", "withdraw"):
            books.approve(entry, OWNER.name)
            if change == "withdraw":
                version, revision = versions(books, entry)
                books.withdraw(
                    entry,
                    OWNER.name,
                    "Check again",
                    expected_version=version,
                    expected_maintenance_revision=revision,
                )
        elif change == "parent_correction":
            home = books.get(entry).session_id
            with scratch.connect() as conn:
                answer = next(
                    t.ordinal
                    for t in store.load_turns(conn, home)
                    if t.kind == "answer" and t.topic_id == TOPIC_IDS[0]
                )
            interviews = make_service(scratch.dsn, agent=None)
            events = await say(
                interviews, home, "Corrected answer.", action="correct", corrects=answer
            )
            assert events[0]["type"] == "corrected", events
        else:
            # Another proposal for the same entry is applied first.
            other = proposals.proposal_id(books.get(entry).session_id, TOPIC_IDS[0])
            version, _ = versions(books, entry)
            with scratch.connect() as conn:
                conn.execute(
                    "INSERT INTO follow_up_proposals (id, session_id, topic_id, entry_id, "
                    "base_version, patch, gap_ids, created_at) SELECT %s, %s, topic_id, entry_id, "
                    "%s, patch, '{}', now() FROM follow_up_proposals WHERE session_id=%s",
                    (other, books.get(entry).session_id, version, done.session_id),
                )
            books.apply_proposal(
                other,
                OWNER.name,
                {"contacts": "merge"},
                expected_revision=1,
                expected_version=version,
            )


@pytest.mark.parametrize(
    "change", ["edit", "reject", "approve", "withdraw", "parent_correction", "other_proposal"]
)
async def test_apply_conflicts_after_concurrent_change(scratch: Scratch, change: str) -> None:
    done = await answered_follow_up(scratch)
    books = RunbookService(scratch.dsn)
    entry = done.world.billing
    pid = proposals.proposal_id(done.session_id, TOPIC_IDS[0])
    version, revision = versions(books, entry)
    with acting_as(OWNER):
        # Start from a draft so that every decision is possible, then refresh to it.
        books.withdraw(
            entry,
            OWNER.name,
            "Start over",
            expected_version=version,
            expected_maintenance_revision=revision,
        )
        version, _ = versions(books, entry)
        books.refresh_proposal(pid, OWNER.name, expected_revision=1, expected_version=version)
    kept = proposal_state(scratch, pid)
    assert kept[:3] == ("pending", 2, version)
    await concurrent_change(scratch, done, books, change)
    current, _ = versions(books, entry)
    assert current > version
    with acting_as(OWNER):
        for expected_version in (version, current):
            with pytest.raises(ConflictError):
                books.apply_proposal(
                    pid,
                    OWNER.name,
                    {"rollback": "merge"},
                    expected_revision=2,
                    expected_version=expected_version,
                )
    assert proposal_state(scratch, pid) == kept, "nothing is written on conflict"
    with acting_as(OWNER):
        if change == "reject":
            with pytest.raises(ConflictError, match="rejected"):
                books.refresh_proposal(
                    pid, OWNER.name, expected_revision=2, expected_version=current
                )
            books.edit(entry, OWNER.name, {"steps": SectionEdit(claims=("Reopened.",))})
            current, _ = versions(books, entry)
        books.refresh_proposal(pid, OWNER.name, expected_revision=2, expected_version=current)
        stored, replayed = books.apply_proposal(
            pid,
            OWNER.name,
            {"rollback": "merge"},
            expected_revision=3,
            expected_version=current,
        )
    assert stored.version == current + 1 and replayed is False
    assert stored.entry.rollback.claims[-1].text == ROLLBACK_ANSWER


# --- P3 / A8 / C1: identical replays and parallel applies ---


async def test_apply_replay_and_parallel_apply(scratch: Scratch) -> None:
    done = await answered_follow_up(scratch)
    entry = done.world.billing
    dsn = make_conninfo(
        scratch.dsn, options=f"-c search_path={scratch.schema},public -c lock_timeout=5s"
    )
    books = RunbookService(dsn)
    pid = proposals.proposal_id(done.session_id, TOPIC_IDS[0])
    version = books.get(entry).version
    choice: dict[Any, Any] = {"rollback": "merge"}
    barrier = Barrier(2)
    outcomes: list[str] = []

    def attempt() -> None:
        with acting_as(OWNER):
            barrier.wait()
            try:
                _, replayed = books.apply_proposal(
                    pid, OWNER.name, choice, expected_revision=1, expected_version=version
                )
                outcomes.append("replayed" if replayed else "applied")
            except ConflictError:
                outcomes.append("conflict")

    threads = [Thread(target=attempt) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(outcomes) in (["applied", "conflict"], ["applied", "replayed"])
    decisions = books.history(entry)
    events = len(audit_details(scratch, "follow_up_apply"))
    with acting_as(OWNER):
        stored, replayed = books.apply_proposal(
            pid, OWNER.name, choice, expected_revision=1, expected_version=version
        )
        assert replayed is True and stored.version == version + 1
        with pytest.raises(ConflictError, match="already applied"):
            books.apply_proposal(
                pid,
                OWNER.name,
                {"contacts": "merge"},
                expected_revision=1,
                expected_version=version,
            )
    assert books.history(entry) == decisions, "a replay records no decision"
    assert len(audit_details(scratch, "follow_up_apply")) == events == 1
    assert [d.action for d in decisions].count("follow_up_apply") == 1


# --- P4 / A7: publication is the ordinary explicit approval ---


def apply_all(books: RunbookService, done: AnsweredFollowUp) -> entry_store.StoredEntry:
    pid = proposals.proposal_id(done.session_id, TOPIC_IDS[0])
    version = books.get(done.world.billing).version
    with acting_as(OWNER):
        stored, _ = books.apply_proposal(
            pid,
            OWNER.name,
            {"rollback": "merge", "contacts": "merge"},
            expected_revision=1,
            expected_version=version,
        )
    return stored


async def test_explicit_approval_publishes_revision_with_follow_up_citations(
    scratch: Scratch,
) -> None:
    done = await answered_follow_up(scratch)
    world, fid = done.world, done.session_id
    books = RunbookService(scratch.dsn)
    applied = apply_all(books, done)
    books.synthesise(world.session_id, overwrite_edits=True)
    assert books.get(world.billing) == applied, "re-synthesis never drops applied facts"
    with acting_as(OWNER):
        approved = books.approve(world.billing, OWNER.name)
    from tests.search_support import index_runbook

    index_runbook(scratch)
    assert approved.version == applied.version + 1
    search = SearchService(scratch.dsn)
    hits = [
        h for h in search.search(READER, "Restore the pre-run export") if h.section == "rollback"
    ]
    assert hits and hits[0].entry.id == str(world.billing)
    assert hits[0].version == approved.version
    citations: Any = hits[0].public()["citations"]
    assert {c["session_id"] for c in citations} == {str(fid)}
    answer = await AnswerService(search).ask(READER, "How do I roll back the billing export?")
    cited = {c.session_id for e in answer.evidence for c in e.citations if hasattr(c, "session_id")}
    assert answer.status == "answered", answer
    assert str(fid) in cited
    markdown = ExportService(scratch.dsn).entry(READER, world.billing).replace("\\", "")
    assert ROLLBACK_ANSWER in markdown and str(fid) in markdown
    with scratch.connect() as conn:
        assert conn.execute(
            "SELECT count(*) FROM runbook_entries e JOIN sessions s ON s.id=e.session_id "
            "JOIN interview_plans p ON p.id=s.plan_id WHERE p.run_id=%s AND e.topic_id=%s "
            "AND e.status='approved'",
            (world.run_id, TOPIC_IDS[0]),
        ).fetchone() == (1,)


# --- P6 / A9: corrections of cited follow-up answers propagate ---


async def correct_follow_up(scratch: Scratch, fid: UUID, turn: int, text: str) -> list[Any]:
    interviews = make_service(scratch.dsn, agent=None)
    with acting_as(OWNER):
        return await say(interviews, fid, text, action="correct", corrects=turn)


async def test_correcting_cited_follow_up_answer_reopens_linked_entry(
    scratch: Scratch, monkeypatch: pytest.MonkeyPatch
) -> None:
    from eki.audit import AuditError
    from eki.runbook import store as module

    done = await answered_follow_up(scratch)
    world, fid = done.world, done.session_id
    books = RunbookService(scratch.dsn)
    applied = apply_all(books, done)
    with acting_as(OWNER):
        approved = books.approve(world.billing, OWNER.name)
    from tests.search_support import index_runbook

    index_runbook(scratch)
    assert SearchService(scratch.dsn).search(READER, "Restore the pre-run export")
    assert index_count(scratch, world.billing) > 0
    answer = turn_of(scratch, fid, ROLLBACK_ANSWER)
    with scratch.connect() as conn:
        turns_before = len(store.load_turns(conn, fid))
        notes_before = store.load_notes(conn, fid)
    monkeypatch.setattr(module, "append_event", lambda *a, **k: (_ for _ in ()).throw(AuditError()))
    with pytest.raises(AuditError):
        await correct_follow_up(scratch, fid, answer, "Restore the export twice.")
    monkeypatch.undo()
    assert books.get(world.billing) == approved, "the failed correction changed nothing"
    with scratch.connect() as conn:
        assert len(store.load_turns(conn, fid)) == turns_before
        assert store.load_notes(conn, fid) == notes_before
    events = await correct_follow_up(scratch, fid, answer, "Restore the export twice.")
    assert events[0]["type"] == "corrected", events
    corrected = books.get(world.billing)
    assert corrected.session_id == world.session_id, "the entry keeps its own home session"
    assert corrected.version == approved.version + 1
    assert corrected.entry.status == "draft" and corrected.entry.approved_by is None
    (claim,) = corrected.entry.rollback.claims
    assert claim.text == "Restore the export twice."
    assert claim.citations[0] == InterviewTurnCitation(
        session_id=str(fid), turn_index=events[0]["turn"]
    )
    assert corrected.entry.contacts == applied.entry.contacts, "unrelated claims are kept"
    assert corrected.entry.trigger == applied.entry.trigger
    assert index_count(scratch, world.billing) == 0
    assert [d.action for d in books.history(world.billing)][-1] == "correction"


async def test_correction_before_apply_rewrites_pending_proposal(scratch: Scratch) -> None:
    done = await answered_follow_up(scratch)
    world, fid = done.world, done.session_id
    books = RunbookService(scratch.dsn)
    pid = proposals.proposal_id(fid, TOPIC_IDS[0])
    version = books.get(world.billing).version
    answer = turn_of(scratch, fid, ROLLBACK_ANSWER)
    events = await correct_follow_up(scratch, fid, answer, "Restore the export twice.")
    assert books.get(world.billing).version == version, "a pending proposal is not the entry"
    status, revision, base_version, patch = proposal_state(scratch, pid)
    assert (status, revision, base_version) == ("pending", 2, version)
    assert [c["text"] for c in patch["rollback"]["claims"]] == ["Restore the export twice."]
    assert patch["rollback"]["claims"][0]["citations"][0]["turn_index"] == events[0]["turn"]
    assert len(patch["contacts"]["claims"]) == 2, "unrelated proposed facts are kept"
    (event,) = [e for e in audit_details(scratch, "correction") if e["target_id"] == str(pid)]
    assert "twice" not in json.dumps(event)
    with acting_as(OWNER):
        with pytest.raises(ConflictError, match="proposal changed"):
            books.apply_proposal(
                pid,
                OWNER.name,
                {"rollback": "merge"},
                expected_revision=1,
                expected_version=version,
            )
        (shown,) = books.proposals_for_session(fid)
        stored, _ = books.apply_proposal(
            pid,
            OWNER.name,
            {"rollback": "merge"},
            expected_revision=shown["revision"],
            expected_version=version,
        )
    assert stored.entry.rollback.claims[-1].text == "Restore the export twice."
    # An applied proposal is immutable history: a later correction leaves it alone.
    contact = turn_of(scratch, fid, "Finance operations lead.")
    await correct_follow_up(scratch, fid, contact, "The finance operations lead.")
    assert proposal_state(scratch, pid)[:2] == ("applied", 3)


# --- P6: faithfulness resolves (session, ordinal), never an ordinal alone ---


def test_check_entry_keys_linked_turns_by_session() -> None:
    home, linked = str(uuid4()), str(uuid4())

    def turn(ordinal: int, text: str, **extra: Any) -> Turn:
        return Turn(
            ordinal=ordinal,
            topic_id="t",
            speaker="expert",
            kind="answer",
            text=RedactedText(text),
            **extra,
        )

    own = [turn(2, "Restart the replicas.")]
    other = [turn(2, "Restore the export."), turn(3, "A secret value.", confidential=True)]

    def entry(*claims: tuple[str, str, int]) -> RunbookEntry:
        return RunbookEntry(
            id=str(uuid4()),
            topic_id="t",
            title="T",
            steps=Section(
                claims=tuple(
                    Claim(
                        text=text,
                        citations=(InterviewTurnCitation(session_id=s, turn_index=n),),
                    )
                    for text, s, n in claims
                )
            ),
        )

    mixed = entry(
        ("Restart the replicas.", home, 2),
        ("Restore the export.", linked, 2),
        ("Restore the export nightly.", linked, 2),
        ("Anything secret.", linked, 3),
    )
    found = check_entry(mixed, own, session_id=home, linked={linked: other})
    assert [(u.index, u.terms) for u in found] == [(2, ("nightly",))]
    assert check_entry(mixed, own, session_id=home) == (), "unlinked citations are skipped"
    legacy = check_entry(entry(("Restart the replicas now.", home, 2)), own)
    assert [u.terms for u in legacy] == [("now",)], "the legacy call is unchanged"


async def test_unsupported_resolves_linked_turns_without_ordinal_collision(
    scratch: Scratch,
) -> None:
    done = await answered_follow_up(scratch)
    world, fid = done.world, done.session_id
    books = RunbookService(scratch.dsn)
    with acting_as(OWNER):
        (shown,) = books.proposals_for_session(fid)
    follow_up = {("contacts", 1), ("contacts", 2), ("rollback", 0)}
    previewed = {(u["section"], u["claim_index"]): u["terms"] for u in shown["unsupported"]}
    assert {k: v for k, v in previewed.items() if k in follow_up} == {
        ("contacts", 2): ["signs", "amounts"]
    }, "follow-up claims are compared with their own follow-up turns"
    assert ("contacts", 0) in previewed, "home claims are still compared with home turns"
    apply_all(books, done)
    found = {
        (u.section, u.index): u.terms
        for u in books.unsupported(world.session_id)[str(world.billing)]
    }
    assert {k: v for k, v in found.items() if k in follow_up} == {
        ("contacts", 2): ("signs", "amounts")
    }
    assert found[("contacts", 0)] == tuple(previewed[("contacts", 0)])
