"""Private runbook entries and their ordered decision history in Postgres."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from eki.audit import AuditDetails, append_event
from eki.ingest.redaction import RedactedText
from eki.runbook.models import RunbookEntry

DecisionAction = Literal["approve", "reject", "edit", "correction"]


@dataclass(frozen=True)
class StoredEntry:
    entry: RunbookEntry
    session_id: UUID
    version: int
    rejection_reason: str | None


@dataclass(frozen=True)
class Decision:
    action: DecisionAction
    who: str
    at: datetime
    reason: str | None


def save_entry(
    conn: psycopg.Connection,
    session_id: UUID,
    entry: RunbookEntry,
    *,
    rejection_reason: RedactedText | None = None,
) -> StoredEntry:
    """Revalidate at the storage boundary. Callers lock the session before changing entries."""
    entry = RunbookEntry.model_validate(entry.model_dump())
    row = conn.execute(
        "INSERT INTO runbook_entries (id, session_id, topic_id, title, status, content, "
        "approved_by, approved_at, rejection_reason) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) "
        "ON CONFLICT (id) DO UPDATE SET title = EXCLUDED.title, status = EXCLUDED.status, "
        "content = EXCLUDED.content, approved_by = EXCLUDED.approved_by, "
        "approved_at = EXCLUDED.approved_at, rejection_reason = EXCLUDED.rejection_reason, "
        "version = runbook_entries.version + 1 RETURNING version",
        (
            entry.id,
            session_id,
            entry.topic_id,
            entry.title,
            entry.status.value,
            Jsonb(entry.model_dump(mode="json")),
            entry.approved_by,
            entry.approved_at,
            rejection_reason,
        ),
    ).fetchone()
    assert row is not None  # noqa: S101 - INSERT RETURNING
    return StoredEntry(entry, session_id, row[0], rejection_reason)


def load_entry(
    conn: psycopg.Connection, entry_id: str, *, lock: bool = False
) -> StoredEntry | None:
    row = conn.execute(
        "SELECT content, session_id, version, rejection_reason FROM runbook_entries WHERE id = %s"  # noqa: S608 - fixed suffix; ID is bound
        + (" FOR UPDATE" if lock else ""),
        (entry_id,),
    ).fetchone()
    return None if row is None else StoredEntry(RunbookEntry.model_validate(row[0]), *row[1:])


def list_entries(conn: psycopg.Connection, session_id: UUID) -> tuple[StoredEntry, ...]:
    rows = conn.execute(
        "SELECT e.content, e.session_id, e.version, e.rejection_reason FROM runbook_entries e "
        "JOIN sessions s ON s.id = e.session_id JOIN plan_topics t "
        "ON t.plan_id = s.plan_id AND t.topic_id = e.topic_id "
        "WHERE e.session_id = %s ORDER BY t.position",
        (session_id,),
    ).fetchall()
    return tuple(StoredEntry(RunbookEntry.model_validate(r[0]), *r[1:]) for r in rows)


def add_decision(
    conn: psycopg.Connection,
    entry_id: str,
    action: DecisionAction,
    who: RedactedText,
    at: datetime,
    reason: RedactedText | None = None,
) -> None:
    row = conn.execute(
        "SELECT p.run_id, e.version FROM runbook_entries e JOIN sessions s ON s.id = e.session_id "
        "JOIN interview_plans p ON p.id = s.plan_id WHERE e.id = %s",
        (entry_id,),
    ).fetchone()
    if row is not None:
        append_event(
            conn,
            "approval" if action == "approve" else "review",
            "completed",
            AuditDetails(operation=action, target_id=UUID(entry_id), version=row[1]),
            run_id=row[0],
        )
    conn.execute(
        "INSERT INTO entry_decisions (entry_id, action, who, at, reason) VALUES (%s,%s,%s,%s,%s)",
        (entry_id, action, who, at, reason),
    )


def decisions(conn: psycopg.Connection, entry_id: str) -> tuple[Decision, ...]:
    rows = conn.execute(
        "SELECT action, who, at, reason FROM entry_decisions WHERE entry_id = %s ORDER BY id",
        (entry_id,),
    ).fetchall()
    return tuple(Decision(*row) for row in rows)
