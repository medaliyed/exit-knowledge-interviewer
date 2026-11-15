"""F21 shared maintenance transitions. Lock order: run, session, entry, flag."""

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import psycopg

from eki.audit import AuditDetails, append_event
from eki.auth import AuthenticationError, ForbiddenError, Principal, acting_as, current_principal
from eki.errors import ConflictError, NotFoundError
from eki.ingest.redaction import redact


def active(conn: psycopg.Connection, principal: Principal) -> None:
    row = conn.execute(
        "SELECT disabled FROM accounts WHERE subject=%s FOR SHARE", (principal.subject,)
    ).fetchone()
    if row and row[0]:
        raise AuthenticationError("account is disabled")


def lock_run(conn: psycopg.Connection, run_id: UUID) -> tuple[Any, ...]:
    row = conn.execute(
        "SELECT "
        "owner_subject,maintenance_started_at,maintenance_revision,expert_name "
        "FROM ingest_runs WHERE id=%s FOR UPDATE",
        (run_id,),
    ).fetchone()
    if row is None:
        raise NotFoundError("run not found")
    return tuple(row)


def reviewer(conn: psycopg.Connection, run_id: UUID, by: str) -> tuple[str, bool]:
    owner, started, _, expert = lock_run(conn, run_id)
    principal = current_principal()
    if principal is not None:
        active(conn, principal)
        if not (principal.subject == owner or (not started and principal.admin)):
            raise NotFoundError("entry not found")
        if by.strip() != principal.name:
            raise ValueError("reviewer must match the authenticated identity")
    if started:
        if principal is None or principal.subject != owner:
            raise NotFoundError("entry not found")
        return principal.name, True
    if by.strip() != expert:
        raise ValueError("reviewer must match the run's expert")
    return expert, False


def capture(conn: psycopg.Connection, run_id: UUID) -> None:
    # Shared session advisory lock survives async commits, released on connection close.
    conn.execute("SELECT pg_advisory_lock_shared(hashtextextended(%s,0))", (f"capture:{run_id}",))
    row = conn.execute(
        "SELECT maintenance_started_at,owner_subject FROM ingest_runs WHERE id=%s", (run_id,)
    ).fetchone()
    if row is None:
        raise NotFoundError("run not found")
    if row[0]:
        raise ConflictError("capture is closed after maintenance handover")
    principal = current_principal()
    if principal:
        active(conn, principal)
        if not (principal.admin or row[1] == principal.subject):
            raise NotFoundError("run not found")


def transfer(
    conn: psycopg.Connection,
    run_id: UUID,
    subject: str,
    actor: Principal,
    *,
    expected_owner: str | None,
    expected_revision: int,
    reason: str,
    start: bool = False,
    identities: Sequence[Principal] = (),
    now: datetime | None = None,
) -> dict[str, Any]:
    if not actor.admin:
        raise ForbiddenError("only an administrator can assign ownership")
    if not subject.strip() or len(subject) > 200 or not reason.strip() or len(reason) > 2000:
        raise ValueError("a valid owner and reason are required")
    now = now or datetime.now(UTC)
    with acting_as(actor), conn.transaction():
        active(conn, actor)
        available = conn.execute(
            "SELECT pg_try_advisory_xact_lock(hashtextextended(%s,0))", (f"capture:{run_id}",)
        ).fetchone()
        if not available or not available[0]:
            raise ConflictError("capture work is running; finish or recover it before handover")
        owner, started, revision, _ = lock_run(conn, run_id)
        if owner != expected_owner or revision != expected_revision:
            raise ConflictError("ownership changed; refresh before transferring")
        target = conn.execute(
            "SELECT name,disabled FROM accounts WHERE subject=%s FOR SHARE", (subject,)
        ).fetchone()
        if (target and target[1]) or (
            not target and subject not in {p.subject for p in identities}
        ):
            raise ValueError("destination must be an active account or configured bearer identity")
        pending = conn.execute(
            "SELECT 1 FROM message_receipts m JOIN sessions s ON s.id=m.session_id "
            "JOIN interview_plans p ON p.id=s.plan_id WHERE p.run_id=%s AND "
            "m.status<>'processed' LIMIT 1",
            (run_id,),
        ).fetchone()
        if pending:
            raise ConflictError("recover accepted interview messages before handover")
        clean = redact(reason, work_email_domains=frozenset()).text
        conn.execute(
            "UPDATE ingest_runs SET owner_subject=%s,maintenance_started_at=CASE WHEN"
            " %s THEN coalesce(maintenance_started_at,%s) ELSE maintenance_started_at"
            " END,maintenance_revision=maintenance_revision+1 WHERE id=%s",
            (subject, start, now, run_id),
        )
        conn.execute(
            "INSERT INTO handovers(run_id,previous_owner,owner_subject,actor_subject,"
            "at,reason,started_maintenance) VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (run_id, owner, subject, actor.subject, now, clean, start and not started),
        )
        append_event(
            conn,
            "access",
            "completed",
            AuditDetails(operation="handover", version=revision + 1),
            run_id=run_id,
        )
    return state(conn, run_id, actor)


def state(conn: psycopg.Connection, run_id: UUID, principal: Principal) -> dict[str, Any]:
    from eki.access import require_manager

    require_manager(conn, run_id, principal)
    row = conn.execute(
        "SELECT r.owner_subject,r.maintenance_started_at,r.maintenance_revision,a"
        ".name,a.disabled FROM ingest_runs r LEFT JOIN accounts a ON "
        "a.subject=r.owner_subject WHERE r.id=%s",
        (run_id,),
    ).fetchone()
    if row is None:
        raise NotFoundError("run not found")
    history = conn.execute(
        "SELECT "
        "previous_owner,owner_subject,actor_subject,at,reason,started_maintenance"
        " FROM handovers WHERE run_id=%s ORDER BY id",
        (run_id,),
    ).fetchall()
    today = datetime.now(UTC).date()
    counts = conn.execute(
        "SELECT count(*) FILTER(WHERE e.status='draft'),count(*) FILTER(WHERE "
        "e.status='approved' AND e.review_by<%s),coalesce(sum((SELECT count(*) "
        "FROM entry_flags f WHERE f.entry_id=e.id AND f.resolution IS NULL)),0) "
        "FROM runbook_entries e JOIN sessions s ON s.id=e.session_id JOIN "
        "interview_plans p ON p.id=s.plan_id WHERE p.run_id=%s",
        (today, run_id),
    ).fetchone()
    return {
        "summary": {
            "drafts": counts[0] if counts else 0,
            "overdue": counts[1] if counts else 0,
            "open_flags": int(counts[2]) if counts else 0,
        },
        "owner_subject": row[0],
        "maintenance_started_at": row[1],
        "run_revision": row[2],
        "owner_name": row[3] or row[0],
        "owner_unavailable": bool(row[4]),
        "history": [
            dict(
                zip(
                    (
                        "previous_owner",
                        "owner_subject",
                        "actor_subject",
                        "at",
                        "reason",
                        "started_maintenance",
                    ),
                    h,
                    strict=True,
                )
            )
            for h in history
        ],
    }


def queue(
    conn: psycopg.Connection,
    run_id: UUID,
    principal: Principal,
    *,
    filter: str = "all",
    limit: int = 50,
    offset: int = 0,
    now: datetime | None = None,
) -> dict[str, Any]:
    from eki.access import require_manager
    from eki.runbook.maintenance import info

    active(conn, principal)
    require_manager(conn, run_id, principal)
    if (
        filter not in ("all", "overdue", "due_soon", "flagged")
        or not 1 <= limit <= 100
        or offset < 0
    ):
        raise ValueError("invalid maintenance filter or page")
    today = (now or datetime.now(UTC)).astimezone(UTC).date()
    rows = conn.execute(
        "SELECT e.id,e.session_id,e.title,e.status,e.version,(SELECT "
        "min(f.created_at) FROM entry_flags f WHERE f.entry_id=e.id AND "
        "f.resolution IS NULL) AS oldest_flag FROM runbook_entries e JOIN "
        "sessions s ON s.id=e.session_id JOIN interview_plans p ON p.id=s.plan_id"
        " WHERE p.run_id=%s AND (%s='all' OR (%s='flagged' AND EXISTS(SELECT 1 "
        "FROM entry_flags f WHERE f.entry_id=e.id AND f.resolution IS NULL)) OR "
        "(e.status='approved' AND ((%s='overdue' AND e.review_by<%s) OR "
        "(%s='due_soon' AND e.review_by BETWEEN %s AND %s::date+14)))) ORDER BY "
        "CASE WHEN e.status='approved' AND e.review_by<%s THEN 0 WHEN "
        "EXISTS(SELECT 1 FROM entry_flags f WHERE f.entry_id=e.id AND "
        "f.resolution IS NULL) THEN 1 ELSE 2 END, CASE WHEN e.review_by<%s THEN "
        "e.review_by END,oldest_flag NULLS LAST,e.review_by NULLS LAST,e.id LIMIT"
        " %s OFFSET %s",
        (run_id, filter, filter, filter, today, filter, today, today, today, today, limit, offset),
    ).fetchall()
    return {
        "entries": [
            {
                "id": str(r[0]),
                "session_id": str(r[1]),
                "title": r[2],
                "status": r[3],
                "version": r[4],
                **info(conn, r[0], now),
            }
            for r in rows
        ],
        "next_offset": offset + limit if len(rows) == limit else None,
    }
