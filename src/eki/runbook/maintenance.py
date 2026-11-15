"""Live entry review metadata; publication state and content version stay independent."""

from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID

import psycopg

from eki.auth import current_principal
from eki.errors import ConflictError, NotFoundError


def deadline(now: datetime, chosen: date | None) -> date:
    day = now.astimezone(UTC).date()
    result = chosen or day + timedelta(days=90)
    if not day < result <= day + timedelta(days=365):
        raise ValueError("review date must be tomorrow through 365 days ahead")
    return result


def freshness(review_by: date, today: date) -> str:
    days = (review_by - today).days
    return "overdue" if days < 0 else "due_soon" if days <= 14 else "current"


def info(
    conn: psycopg.Connection, entry_id: UUID | str, now: datetime | None = None
) -> dict[str, Any]:
    row = conn.execute(
        "SELECT e.status,e.maintenance_revision,e.review_by,e.last_reviewed_at,e."
        "last_reviewed_by_subject,e.last_reviewed_by_name,e.reviewed_version,r.ow"
        "ner_subject,a.name, (SELECT count(*) FROM entry_flags f WHERE "
        "f.entry_id=e.id AND f.resolution IS "
        "NULL),r.expert_name,r.maintenance_started_at FROM runbook_entries e JOIN"
        " "
        "sessions s ON s.id=e.session_id JOIN interview_plans p ON p.id=s.plan_id"
        " JOIN ingest_runs r ON r.id=p.run_id LEFT JOIN accounts a ON "
        "a.subject=r.owner_subject WHERE e.id=%s",
        (entry_id,),
    ).fetchone()
    if row is None:
        raise NotFoundError("entry not found")
    principal = current_principal()
    can_review = bool(
        principal
        and (
            principal.subject == row[7]
            if row[11]
            else (principal.admin or principal.subject == row[7]) and principal.name == row[10]
        )
    )
    return {
        "can_review": can_review,
        "maintenance_started_at": None if row[11] is None else row[11].isoformat(),
        "maintenance_revision": row[1],
        "review_by": None if row[2] is None else row[2].isoformat(),
        "last_reviewed_at": None if row[3] is None else row[3].isoformat(),
        "last_reviewed_by_subject": row[4],
        "last_reviewed_by_name": row[5],
        "reviewed_version": row[6],
        "owner_subject": row[7],
        "owner_name": row[8] or row[7],
        "open_flag_count": row[9],
        "freshness": freshness(row[2], (now or datetime.now(UTC)).astimezone(UTC).date())
        if row[0] == "approved" and row[2]
        else None,
    }


def expected(
    conn: psycopg.Connection,
    entry_id: UUID | str,
    version: int,
    expected_version: int | None,
    expected_revision: int | None,
    *,
    required: bool,
) -> None:
    current = conn.execute(
        "SELECT maintenance_revision FROM runbook_entries WHERE id=%s", (entry_id,)
    ).fetchone()
    if required and (expected_version is None or expected_revision is None):
        raise ConflictError("entry version and maintenance revision are required")
    if (expected_version is not None and expected_version != version) or (
        expected_revision is not None and (current is None or current[0] != expected_revision)
    ):
        raise ConflictError("entry changed; refresh before reviewing")


def reviewed(
    conn: psycopg.Connection, entry_id: UUID | str, by: str, now: datetime, chosen: date | None
) -> None:
    principal = current_principal()
    conn.execute(
        "UPDATE runbook_entries SET review_by=%s,last_reviewed_at=%s,last_reviewe"
        "d_by_subject=%s,last_reviewed_by_name=%s,reviewed_version=version,mainte"
        "nance_revision=maintenance_revision+1 WHERE id=%s",
        (
            deadline(now, chosen),
            now,
            None if principal is None else principal.subject,
            by,
            entry_id,
        ),
    )
