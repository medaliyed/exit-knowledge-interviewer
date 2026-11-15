"""F21 shared maintenance transitions. Lock order: run, session, entry, flag."""

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import psycopg

from eki.audit import AuditDetails, append_event
from eki.auth import AuthenticationError, ForbiddenError, Principal, current_principal
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
    with conn.transaction():
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
    return {
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
