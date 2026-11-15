"""Private runbook entries and their ordered decision history in Postgres."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from eki.audit import AuditDetails, append_event
from eki.auth import current_principal
from eki.ingest.redaction import RedactedText
from eki.runbook.models import RunbookEntry

DecisionAction = Literal["approve", "reject", "edit", "correction", "review", "withdraw"]


@dataclass(frozen=True)
class StoredEntry:
    entry: RunbookEntry
    session_id: UUID
    version: int
    rejection_reason: str | None
    edited: bool = False
    """The expert changed it by hand; synthesis must not replace it unless told to."""


@dataclass(frozen=True)
class Decision:
    action: DecisionAction
    who: str
    at: datetime
    reason: str | None
    actor_subject: str | None = None
    version: int | None = None


def save_entry(
    conn: psycopg.Connection,
    session_id: UUID,
    entry: RunbookEntry,
    *,
    rejection_reason: RedactedText | None = None,
    edited: bool | None = None,
) -> StoredEntry:
    """Revalidate at the storage boundary. Callers lock the session before changing entries.

    ``edited`` sets the hand-edited flag; ``None`` keeps whatever the entry already has.
    """
    entry = RunbookEntry.model_validate(entry.model_dump())
    row = conn.execute(
        "INSERT INTO runbook_entries (id, session_id, topic_id, title, status, content, "
        "approved_by, approved_at, rejection_reason, edited, "
        "review_by,last_reviewed_at,last_reviewed_by_name,reviewed_version) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,coalesce(%s, false),%s,%s,%s,CASE "
        "WHEN %s THEN 1 ELSE NULL END) "
        "ON CONFLICT (id) DO UPDATE SET title = EXCLUDED.title, status = EXCLUDED.status, "
        "content = EXCLUDED.content, approved_by = EXCLUDED.approved_by, "
        "approved_at = EXCLUDED.approved_at, rejection_reason = EXCLUDED.rejection_reason, "
        "edited = coalesce(%s, runbook_entries.edited), "
        "maintenance_revision=runbook_entries.maintenance_revision+1, "
        "review_by=CASE WHEN EXCLUDED.status='approved' AND "
        "(runbook_entries.status<>'approved' OR runbook_entries.approved_at IS "
        "DISTINCT FROM EXCLUDED.approved_at) THEN EXCLUDED.review_by ELSE "
        "runbook_entries.review_by END, "
        "last_reviewed_at=CASE WHEN EXCLUDED.status='approved' AND "
        "(runbook_entries.status<>'approved' OR runbook_entries.approved_at IS "
        "DISTINCT FROM EXCLUDED.approved_at) THEN EXCLUDED.last_reviewed_at ELSE "
        "runbook_entries.last_reviewed_at END, "
        "last_reviewed_by_name=CASE WHEN EXCLUDED.status='approved' AND "
        "(runbook_entries.status<>'approved' OR runbook_entries.approved_at IS "
        "DISTINCT FROM EXCLUDED.approved_at) THEN EXCLUDED.last_reviewed_by_name "
        "ELSE runbook_entries.last_reviewed_by_name END, "
        "reviewed_version=CASE WHEN EXCLUDED.status='approved' AND "
        "(runbook_entries.status<>'approved' OR runbook_entries.approved_at IS "
        "DISTINCT FROM EXCLUDED.approved_at) THEN runbook_entries.version+1 ELSE "
        "runbook_entries.reviewed_version END, "
        "version = runbook_entries.version + 1 RETURNING version, edited",
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
            edited,
            None
            if entry.approved_at is None
            else entry.approved_at.astimezone(UTC).date() + timedelta(days=90),
            entry.approved_at,
            entry.approved_by,
            entry.status == "approved",
            edited,
        ),
    ).fetchone()
    assert row is not None  # noqa: S101 - INSERT RETURNING
    return StoredEntry(entry, session_id, row[0], rejection_reason, row[1])


def load_entry(
    conn: psycopg.Connection, entry_id: str, *, lock: bool = False
) -> StoredEntry | None:
    row = conn.execute(
        "SELECT content, session_id, version, rejection_reason, edited "
        "FROM runbook_entries WHERE id = %s" + (" FOR UPDATE" if lock else ""),
        (entry_id,),
    ).fetchone()
    return None if row is None else StoredEntry(RunbookEntry.model_validate(row[0]), *row[1:])


def list_entries(conn: psycopg.Connection, session_id: UUID) -> tuple[StoredEntry, ...]:
    rows = conn.execute(
        "SELECT e.content, e.session_id, e.version, e.rejection_reason, e.edited "
        "FROM runbook_entries e "
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
    principal = current_principal()
    conn.execute(
        "INSERT INTO entry_decisions (entry_id, action, who, at, "
        "reason,actor_subject,version) VALUES (%s,%s,%s,%s,%s,%s,%s)",
        (
            entry_id,
            action,
            who,
            at,
            reason,
            None if principal is None else principal.subject,
            None if row is None else row[1],
        ),
    )


def decisions(conn: psycopg.Connection, entry_id: str) -> tuple[Decision, ...]:
    rows = conn.execute(
        "SELECT action, who, at, reason, actor_subject, version FROM "
        "entry_decisions WHERE entry_id = %s ORDER BY id",
        (entry_id,),
    ).fetchall()
    return tuple(Decision(*row) for row in rows)
