"""Task P2-b: plans, sessions, turns, notes and open items in Postgres."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

import psycopg
import pytest

from eki.ingest.pipeline import store as store_snapshot
from eki.ingest.redaction import RedactedText
from eki.interview import store
from eki.interview.models import SECTIONS, InterviewPlan
from tests import builders
from tests.test_interview_models import drafted, text, topic

pytestmark = pytest.mark.db


@pytest.fixture
def run_id(conn: psycopg.Connection) -> UUID:
    return store_snapshot(conn, builders.snapshot([builders.ticket("TCK-0006", "billing", "x")]))


def saved_plan(conn: psycopg.Connection, run_id: UUID, *topic_ids: str) -> InterviewPlan:
    ids = topic_ids or ("b-topic", "a-topic")
    plan = InterviewPlan(id=uuid4(), run_id=run_id, topics=tuple(topic(i) for i in ids))
    store.save_plan(conn, plan)
    return plan


def open_session(conn: psycopg.Connection, run_id: UUID) -> UUID:
    plan = saved_plan(conn, run_id)
    return store.create_session(conn, uuid4(), plan.id)


def test_plan_round_trips_in_order(conn: psycopg.Connection, run_id: UUID) -> None:
    extra = topic(
        "expert:1",
        origin="expert",
        score=None,
        artifact_ids=(),
        questions=(
            drafted().model_copy(update={"origin": "expert", "artifact_id": None, "quote": None}),
        ),
        dropped=("quote not found in TCK-0006",),
    )
    plan = InterviewPlan(id=uuid4(), run_id=run_id, topics=(topic("b"), extra, topic("a")))
    store.save_plan(conn, plan)
    assert store.load_plan(conn, plan.id) == plan


def test_saving_again_replaces_topics_and_keeps_the_session(
    conn: psycopg.Connection, run_id: UUID
) -> None:
    plan = saved_plan(conn, run_id, "a", "b")
    session_id = store.create_session(conn, uuid4(), plan.id)
    now = datetime(2026, 11, 1, 12, tzinfo=UTC)
    approved = plan.model_copy(
        update={
            "topics": plan.topics[:1],
            "status": "approved",
            "approved_by": "Nadia",
            "approved_at": now,
        }
    )
    store.save_plan(conn, approved)
    assert store.load_plan(conn, plan.id) == approved
    assert store.load_session(conn, session_id) is not None


def test_unknown_plan_and_session_are_none(conn: psycopg.Connection) -> None:
    assert store.load_plan(conn, uuid4()) is None
    assert store.load_session(conn, uuid4()) is None


def test_session_round_trip_and_progress(conn: psycopg.Connection, run_id: UUID) -> None:
    plan = saved_plan(conn, run_id)
    session_id = store.create_session(conn, uuid4(), plan.id)
    assert store.create_session(conn, uuid4(), plan.id) == session_id, "one session per plan"
    session = store.load_session(conn, session_id)
    assert session is not None
    assert (session.plan_id, session.run_id, session.status) == (plan.id, run_id, "active")
    store.set_current_topic(conn, session_id, "a-topic")
    store.finish_session(conn, session_id)
    session = store.load_session(conn, session_id)
    assert session is not None
    assert (session.current_topic_id, session.status) == (None, "finished")


def test_turns_get_consecutive_ordinals(conn: psycopg.Connection, run_id: UUID) -> None:
    session_id = open_session(conn, run_id)
    q = store.add_question(conn, session_id, "a-topic", text("Why?"), section="rationale", step=1)
    a = store.add_expert_turn(conn, session_id, "a-topic", "answer", text("Because."), answers=q)
    assert (q, a) == (1, 2)
    turns = store.load_turns(conn, session_id)
    assert [(t.ordinal, t.speaker, t.kind, t.text) for t in turns] == [
        (1, "interviewer", "question", "Why?"),
        (2, "expert", "answer", "Because."),
    ]
    assert turns[0].section == "rationale"
    assert turns[1].answers == 1


def test_retried_writes_do_not_duplicate_turns(conn: psycopg.Connection, run_id: UUID) -> None:
    session_id = open_session(conn, run_id)
    first = store.add_question(
        conn, session_id, "a-topic", text("Why?"), section="rationale", step=4
    )
    again = store.add_question(
        conn, session_id, "a-topic", text("Why so?"), section="steps", step=4
    )
    answer = store.add_expert_turn(
        conn, session_id, "a-topic", "answer", text("one"), answers=first
    )
    retried = store.add_expert_turn(
        conn, session_id, "a-topic", "answer", text("two"), answers=first
    )
    assert first == again
    assert answer == retried
    assert [t.text for t in store.load_turns(conn, session_id)] == ["Why so?", "two"]


def test_confidential_turn_round_trips_with_its_flag(
    conn: psycopg.Connection, run_id: UUID
) -> None:
    session_id = open_session(conn, run_id)
    q = store.add_question(conn, session_id, "a-topic", text("Who?"), section="contacts", step=1)
    store.add_expert_turn(
        conn, session_id, "a-topic", "answer", text("Ask Marek."), answers=q, confidential=True
    )
    turn = store.load_turns(conn, session_id)[1]
    assert turn.confidential is True
    assert turn.text == "Ask Marek."


def test_notes_open_items_and_checklist(conn: psycopg.Connection, run_id: UUID) -> None:
    session_id = open_session(conn, run_id)
    q = store.add_question(conn, session_id, "a-topic", text("How?"), section="steps", step=1)
    a = store.add_expert_turn(conn, session_id, "a-topic", "answer", text("Restart."), answers=q)
    store.mark_open(conn, session_id, "a-topic", "steps", text("not asked yet"), turn=None)
    store.mark_open(conn, session_id, "a-topic", "rollback", text("she does not remember"), turn=a)
    store.record_note(conn, session_id, "a-topic", "steps", text("Restart replicas."), turn=a)
    store.record_note(conn, session_id, "a-topic", "contacts", text("-"), turn=a, confidential=True)
    checklist = store.checklist(conn, session_id, "a-topic")
    assert list(checklist) == list(SECTIONS)
    assert checklist["steps"] == "noted", "a note replaces the open item"
    assert checklist["rollback"] == "open"
    assert checklist["contacts"] == "confidential"
    assert checklist["trigger"] == "missing"
    assert [(o.section, o.reason, o.turn) for o in store.load_open_items(conn, session_id)] == [
        ("rollback", "she does not remember", a)
    ]
    assert store.checklist(conn, session_id, "b-topic")["steps"] == "missing"


def test_a_new_note_supersedes_the_old_one(conn: psycopg.Connection, run_id: UUID) -> None:
    session_id = open_session(conn, run_id)
    q = store.add_question(conn, session_id, "a-topic", text("How?"), section="steps", step=1)
    a = store.add_expert_turn(conn, session_id, "a-topic", "answer", text("Restart."), answers=q)
    store.record_note(conn, session_id, "a-topic", "steps", text("first"), turn=a)
    store.record_note(conn, session_id, "a-topic", "steps", text("second"), turn=a)
    assert [n.text for n in store.load_notes(conn, session_id)] == ["second"]
    everything = store.load_notes(conn, session_id, include_superseded=True)
    assert [(n.text, n.superseded) for n in everything] == [("first", True), ("second", False)]


def test_supersede_notes_from_a_turn(conn: psycopg.Connection, run_id: UUID) -> None:
    session_id = open_session(conn, run_id)
    q = store.add_question(conn, session_id, "a-topic", text("How?"), section="steps", step=1)
    a = store.add_expert_turn(conn, session_id, "a-topic", "answer", text("Workers."), answers=q)
    store.record_note(conn, session_id, "a-topic", "steps", text("Workers first."), turn=a)
    store.record_note(conn, session_id, "a-topic", "pitfalls", text("Slow."), turn=a)
    fix = store.add_expert_turn(
        conn, session_id, "a-topic", "correction", text("Replicas first."), corrects=a
    )
    changed = store.replace_notes_from_turn(conn, session_id, a, text("Replicas first."), fix)
    assert changed == (("a-topic", "pitfalls"), ("a-topic", "steps"))
    assert {(n.section, n.text, n.turn) for n in store.load_notes(conn, session_id)} == {
        ("steps", "Replicas first.", fix),
        ("pitfalls", "Replicas first.", fix),
    }
    assert len(store.load_turns(conn, session_id)) == 3, "both turns stay in the transcript"


def test_deleting_a_run_removes_its_interview_data(conn: psycopg.Connection, run_id: UUID) -> None:
    session_id = open_session(conn, run_id)
    q = store.add_question(conn, session_id, "a-topic", text("How?"), section="steps", step=1)
    a = store.add_expert_turn(conn, session_id, "a-topic", "answer", text("Restart."), answers=q)
    store.record_note(conn, session_id, "a-topic", "steps", text("Restart."), turn=a)
    store.mark_open(conn, session_id, "a-topic", "rollback", text("unknown"), turn=a)
    conn.execute("DELETE FROM ingest_runs WHERE id = %s", (run_id,))
    for table in (
        "interview_plans",
        "plan_topics",
        "questions",
        "sessions",
        "turns",
        "notes",
        "open_items",
    ):
        count = conn.execute(f"SELECT count(*) FROM {table}").fetchone()  # noqa: S608
        assert count == (0,), table


def test_session_lock_is_exclusive(conn: psycopg.Connection, run_id: UUID) -> None:
    from eki.config import get_settings
    from eki.db import connect

    key = uuid4()
    assert store.try_lock(conn, key) is True
    other = connect(get_settings())
    try:
        assert store.try_lock(other, key) is False
        store.unlock(conn, key)
        assert store.try_lock(other, key) is True
    finally:
        other.close()


def test_stored_text_is_typed_as_redacted(conn: psycopg.Connection, run_id: UUID) -> None:
    session_id = open_session(conn, run_id)
    store.add_question(conn, session_id, None, RedactedText("Hello."), section=None, step=1)
    assert store.load_turns(conn, session_id)[0].topic_id is None
