"""Plans, sessions, turns, notes and open items in Postgres.

Writes are safe to repeat: a workflow step that is retried after a failure
rewrites its own rows instead of adding new ones. Functions accept only
``RedactedText`` for anything a person or a model wrote.
"""

from typing import Literal
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from eki.ingest.redaction import RedactedText
from eki.interview.models import (
    SECTIONS,
    InterviewPlan,
    Note,
    OpenItem,
    PlannedTopic,
    Question,
    SectionStatus,
    Session,
    Turn,
)
from eki.runbook.models import SectionName


def _stored(value: str) -> RedactedText:
    # Only this module writes these tables, and it accepts nothing but redacted text.
    return RedactedText(value)


def run_exists(conn: psycopg.Connection, run_id: UUID) -> bool:
    row = conn.execute(
        "SELECT 1 FROM ingest_runs WHERE id = %s AND status = 'completed'", (run_id,)
    ).fetchone()
    return row is not None


def save_plan(conn: psycopg.Connection, plan: InterviewPlan) -> None:
    """Insert or replace a plan with its topics and questions, atomically."""
    with conn.transaction():
        conn.execute(
            "INSERT INTO interview_plans (id, run_id, status, approved_by, approved_at) "
            "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (id) DO UPDATE SET "
            "status = EXCLUDED.status, approved_by = EXCLUDED.approved_by, "
            "approved_at = EXCLUDED.approved_at",
            (plan.id, plan.run_id, plan.status, plan.approved_by, plan.approved_at),
        )
        conn.execute("DELETE FROM plan_topics WHERE plan_id = %s", (plan.id,))
        with conn.cursor() as cursor:
            cursor.executemany(
                "INSERT INTO plan_topics (plan_id, position, topic_id, label, kind, system, "
                "origin, score, reason, artifact_ids, dropped) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                [
                    (
                        plan.id,
                        position,
                        t.topic_id,
                        t.label,
                        t.kind,
                        t.system,
                        t.origin,
                        t.score,
                        t.reason,
                        Jsonb(list(t.artifact_ids)),
                        Jsonb(list(t.dropped)),
                    )
                    for position, t in enumerate(plan.topics)
                ],
            )
            cursor.executemany(
                "INSERT INTO questions (plan_id, topic_id, position, text, origin, artifact_id, "
                "quote) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                [
                    (plan.id, t.topic_id, position, q.text, q.origin, q.artifact_id, q.quote)
                    for t in plan.topics
                    for position, q in enumerate(t.questions)
                ],
            )


def load_plan(conn: psycopg.Connection, plan_id: UUID) -> InterviewPlan | None:
    head = conn.execute(
        "SELECT run_id, status, approved_by, approved_at FROM interview_plans WHERE id = %s",
        (plan_id,),
    ).fetchone()
    if head is None:
        return None
    questions: dict[str, list[Question]] = {}
    for topic_id, text, origin, artifact_id, quote in conn.execute(
        "SELECT topic_id, text, origin, artifact_id, quote FROM questions "
        "WHERE plan_id = %s ORDER BY topic_id, position",
        (plan_id,),
    ).fetchall():
        questions.setdefault(topic_id, []).append(
            Question(
                text=_stored(text),
                origin=origin,
                artifact_id=artifact_id,
                quote=None if quote is None else _stored(quote),
            )
        )
    topics = tuple(
        PlannedTopic(
            topic_id=r[0],
            label=r[1],
            kind=r[2],
            system=r[3],
            origin=r[4],
            score=r[5],
            reason=r[6],
            artifact_ids=tuple(r[7]),
            dropped=tuple(r[8]),
            questions=tuple(questions.get(r[0], [])),
        )
        for r in conn.execute(
            "SELECT topic_id, label, kind, system, origin, score, reason, artifact_ids, dropped "
            "FROM plan_topics WHERE plan_id = %s ORDER BY position",
            (plan_id,),
        ).fetchall()
    )
    return InterviewPlan(
        id=plan_id,
        run_id=head[0],
        status=head[1],
        approved_by=head[2],
        approved_at=head[3],
        topics=topics,
    )


def create_session(conn: psycopg.Connection, session_id: UUID, plan_id: UUID) -> UUID:
    """Open the session of a plan. A plan has one session: a second call returns the first ID."""
    with conn.transaction():
        conn.execute(
            "INSERT INTO sessions (id, plan_id, status) VALUES (%s, %s, 'active') "
            "ON CONFLICT (plan_id) DO NOTHING",
            (session_id, plan_id),
        )
        row = conn.execute("SELECT id FROM sessions WHERE plan_id = %s", (plan_id,)).fetchone()
    assert row is not None  # noqa: S101 - just inserted or already there
    return UUID(str(row[0]))


def load_session(conn: psycopg.Connection, session_id: UUID) -> Session | None:
    row = conn.execute(
        "SELECT s.plan_id, p.run_id, s.status, s.current_topic_id FROM sessions s "
        "JOIN interview_plans p ON p.id = s.plan_id WHERE s.id = %s",
        (session_id,),
    ).fetchone()
    if row is None:
        return None
    return Session(
        id=session_id, plan_id=row[0], run_id=row[1], status=row[2], current_topic_id=row[3]
    )


def session_of_plan(conn: psycopg.Connection, plan_id: UUID) -> UUID | None:
    row = conn.execute("SELECT id FROM sessions WHERE plan_id = %s", (plan_id,)).fetchone()
    return None if row is None else UUID(str(row[0]))


def set_current_topic(conn: psycopg.Connection, session_id: UUID, topic_id: str | None) -> None:
    with conn.transaction():
        conn.execute(
            "UPDATE sessions SET current_topic_id = %s WHERE id = %s", (topic_id, session_id)
        )


def finish_session(conn: psycopg.Connection, session_id: UUID) -> None:
    with conn.transaction():
        conn.execute(
            "UPDATE sessions SET status = 'finished', current_topic_id = NULL, "
            "finished_at = coalesce(finished_at, now()) WHERE id = %s",
            (session_id,),
        )


def add_question(
    conn: psycopg.Connection,
    session_id: UUID,
    topic_id: str | None,
    text: RedactedText,
    *,
    section: SectionName | None,
    step: int,
) -> int:
    """Store an interviewer question and return its ordinal. One question per workflow step."""
    with conn.transaction():
        conn.execute("SELECT 1 FROM sessions WHERE id = %s FOR UPDATE", (session_id,))
        row = conn.execute(
            "INSERT INTO turns (session_id, ordinal, topic_id, speaker, kind, text, section, step) "
            "VALUES (%s, (SELECT coalesce(max(ordinal), 0) + 1 FROM turns WHERE session_id = %s), "
            "%s, 'interviewer', 'question', %s, %s, %s) "
            "ON CONFLICT (session_id, step) WHERE step IS NOT NULL DO UPDATE SET "
            "text = EXCLUDED.text, section = EXCLUDED.section, topic_id = EXCLUDED.topic_id "
            "RETURNING ordinal",
            (session_id, session_id, topic_id, text, section, step),
        ).fetchone()
    assert row is not None  # noqa: S101
    return int(row[0])


def add_expert_turn(
    conn: psycopg.Connection,
    session_id: UUID,
    topic_id: str | None,
    kind: Literal["answer", "skip", "correction"],
    text: RedactedText,
    *,
    answers: int | None = None,
    corrects: int | None = None,
    confidential: bool = False,
) -> int:
    """Store what the expert said and return its ordinal. One answer or skip per question."""
    with conn.transaction():
        conn.execute("SELECT 1 FROM sessions WHERE id = %s FOR UPDATE", (session_id,))
        row = conn.execute(
            "INSERT INTO turns (session_id, ordinal, topic_id, speaker, kind, text, confidential, "
            "answers, corrects) VALUES (%s, "
            "(SELECT coalesce(max(ordinal), 0) + 1 FROM turns WHERE session_id = %s), "
            "%s, 'expert', %s, %s, %s, %s, %s) "
            "ON CONFLICT (session_id, answers) WHERE answers IS NOT NULL DO UPDATE SET "
            "kind = EXCLUDED.kind, text = EXCLUDED.text, confidential = EXCLUDED.confidential "
            "RETURNING ordinal",
            (session_id, session_id, topic_id, kind, text, confidential, answers, corrects),
        ).fetchone()
    assert row is not None  # noqa: S101
    return int(row[0])


def load_turns(conn: psycopg.Connection, session_id: UUID) -> tuple[Turn, ...]:
    rows = conn.execute(
        "SELECT ordinal, topic_id, speaker, kind, text, confidential, section, answers, corrects "
        "FROM turns WHERE session_id = %s ORDER BY ordinal",
        (session_id,),
    ).fetchall()
    return tuple(
        Turn(
            ordinal=r[0],
            topic_id=r[1],
            speaker=r[2],
            kind=r[3],
            text=_stored(r[4]),
            confidential=r[5],
            section=r[6],
            answers=r[7],
            corrects=r[8],
        )
        for r in rows
    )


def load_turn(conn: psycopg.Connection, session_id: UUID, ordinal: int) -> Turn | None:
    return next((t for t in load_turns(conn, session_id) if t.ordinal == ordinal), None)


def waiting_question(conn: psycopg.Connection, session_id: UUID) -> Turn | None:
    """The interviewer's latest question, if the expert has not responded to it yet."""
    turns = load_turns(conn, session_id)
    questions = [t for t in turns if t.kind == "question"]
    if not questions:
        return None
    last = questions[-1]
    answered = any(t.answers == last.ordinal for t in turns)
    return None if answered else last


def record_note(
    conn: psycopg.Connection,
    session_id: UUID,
    topic_id: str,
    section: SectionName,
    text: RedactedText,
    *,
    turn: int,
    confidential: bool = False,
) -> None:
    """Put a note in force for a section. An earlier note is kept but superseded."""
    with conn.transaction():
        conn.execute(
            "UPDATE notes SET superseded = true WHERE session_id = %s AND topic_id = %s "
            "AND section = %s AND NOT superseded",
            (session_id, topic_id, section),
        )
        conn.execute(
            "INSERT INTO notes (session_id, topic_id, section, text, turn_ordinal, confidential) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (session_id, topic_id, section, text, turn, confidential),
        )
        conn.execute(
            "DELETE FROM open_items WHERE session_id = %s AND topic_id = %s AND section = %s",
            (session_id, topic_id, section),
        )


def mark_open(
    conn: psycopg.Connection,
    session_id: UUID,
    topic_id: str,
    section: SectionName,
    reason: RedactedText,
    *,
    turn: int | None,
) -> None:
    """Record that a section could not be filled, and why."""
    with conn.transaction():
        conn.execute(
            "INSERT INTO open_items (session_id, topic_id, section, reason, turn_ordinal) "
            "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (session_id, topic_id, section) "
            "DO UPDATE SET reason = EXCLUDED.reason, turn_ordinal = EXCLUDED.turn_ordinal",
            (session_id, topic_id, section, reason, turn),
        )


def replace_notes_from_turn(
    conn: psycopg.Connection,
    session_id: UUID,
    corrected_turn: int,
    text: RedactedText,
    correction_turn: int,
) -> tuple[tuple[str, SectionName], ...]:
    """Supersede every note taken from one turn with the expert's correction, word for word.

    Returns the (topic, section) pairs that changed, sorted.
    """
    with conn.transaction():
        rows = conn.execute(
            "UPDATE notes SET superseded = true WHERE session_id = %s AND turn_ordinal = %s "
            "AND NOT superseded RETURNING topic_id, section",
            (session_id, corrected_turn),
        ).fetchall()
        changed = tuple(sorted((str(r[0]), r[1]) for r in rows))
        with conn.cursor() as cursor:
            cursor.executemany(
                "INSERT INTO notes (session_id, topic_id, section, text, turn_ordinal) "
                "VALUES (%s, %s, %s, %s, %s)",
                [
                    (session_id, topic_id, section, text, correction_turn)
                    for topic_id, section in changed
                ],
            )
    return changed


def load_notes(
    conn: psycopg.Connection, session_id: UUID, *, include_superseded: bool = False
) -> tuple[Note, ...]:
    rows = conn.execute(
        "SELECT topic_id, section, text, turn_ordinal, confidential, superseded FROM notes "
        "WHERE session_id = %s AND (%s OR NOT superseded) ORDER BY id",
        (session_id, include_superseded),
    ).fetchall()
    return tuple(
        Note(
            topic_id=r[0],
            section=r[1],
            text=_stored(r[2]),
            turn=r[3],
            confidential=r[4],
            superseded=r[5],
        )
        for r in rows
    )


def load_open_items(conn: psycopg.Connection, session_id: UUID) -> tuple[OpenItem, ...]:
    rows = conn.execute(
        "SELECT topic_id, section, reason, turn_ordinal FROM open_items WHERE session_id = %s "
        'ORDER BY topic_id COLLATE "C", section COLLATE "C"',
        (session_id,),
    ).fetchall()
    return tuple(
        OpenItem(topic_id=r[0], section=r[1], reason=_stored(r[2]), turn=r[3]) for r in rows
    )


def checklist(
    conn: psycopg.Connection, session_id: UUID, topic_id: str
) -> dict[SectionName, SectionStatus]:
    """The state of each of the seven sections of one topic, in template order."""
    status: dict[SectionName, SectionStatus] = dict.fromkeys(SECTIONS, "missing")
    for section, _ in conn.execute(
        "SELECT section, 1 FROM open_items WHERE session_id = %s AND topic_id = %s",
        (session_id, topic_id),
    ).fetchall():
        status[section] = "open"
    for section, confidential in conn.execute(
        "SELECT section, confidential FROM notes WHERE session_id = %s AND topic_id = %s "
        "AND NOT superseded",
        (session_id, topic_id),
    ).fetchall():
        status[section] = "confidential" if confidential else "noted"
    return status


def try_lock(conn: psycopg.Connection, key: UUID) -> bool:
    """Take an exclusive lock that lasts until ``unlock`` or the connection closes."""
    row = conn.execute(
        "SELECT pg_try_advisory_lock(hashtextextended(%s, 0))", (str(key),)
    ).fetchone()
    return bool(row and row[0])


def unlock(conn: psycopg.Connection, key: UUID) -> None:
    conn.execute("SELECT pg_advisory_unlock(hashtextextended(%s, 0))", (str(key),))
