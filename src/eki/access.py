"""Run-level ownership and approved-entry readership, shared by every read surface."""

from uuid import UUID

import psycopg

from eki.audit import AuditDetails, append_event
from eki.auth import ForbiddenError, Principal
from eki.errors import NotFoundError
from eki.runbook.models import RunbookEntry
from eki.runbook.store import StoredEntry

# Embed this fixed predicate in queries where r is ingest_runs. Values are always bound.
VISIBLE_RUN = """(%s OR r.owner_subject = %s OR EXISTS (
    SELECT 1 FROM run_readers acl WHERE acl.run_id = r.id AND acl.subject = %s))"""


def visible_params(principal: Principal) -> tuple[bool, str, str]:
    return principal.admin, principal.subject, principal.subject


def require_manager(conn: psycopg.Connection, run_id: UUID, principal: Principal) -> None:
    row = conn.execute("SELECT owner_subject FROM ingest_runs WHERE id = %s", (run_id,)).fetchone()
    if row is None or not (principal.admin or row[0] == principal.subject):
        raise NotFoundError("run not found")


def resource_run(conn: psycopg.Connection, resource: str, resource_id: UUID) -> UUID | None:
    queries = {
        "runs": "SELECT id FROM ingest_runs WHERE id = %s",
        "plans": "SELECT run_id FROM interview_plans WHERE id = %s",
        "sessions": (
            "SELECT p.run_id FROM sessions s JOIN interview_plans p "
            "ON p.id = s.plan_id WHERE s.id = %s"
        ),
        "entries": (
            "SELECT p.run_id FROM runbook_entries e JOIN sessions s ON s.id = e.session_id "
            "JOIN interview_plans p ON p.id = s.plan_id WHERE e.id = %s"
        ),
    }
    row = conn.execute(queries[resource], (resource_id,)).fetchone()
    return None if row is None else UUID(str(row[0]))


def approved_entry(conn: psycopg.Connection, entry_id: UUID, principal: Principal) -> StoredEntry:
    row = conn.execute(
        "SELECT e.content, e.session_id, e.version FROM runbook_entries e "  # noqa: S608 - bound values
        "JOIN sessions s ON s.id = e.session_id JOIN interview_plans p ON p.id = s.plan_id "
        "JOIN ingest_runs r ON r.id = p.run_id WHERE e.id = %s AND e.status = 'approved' AND "
        + VISIBLE_RUN,
        (entry_id, *visible_params(principal)),
    ).fetchone()
    if row is None:
        raise NotFoundError("entry not found")
    return StoredEntry(RunbookEntry.model_validate(row[0]), row[1], row[2], None)


def set_owner(conn: psycopg.Connection, run_id: UUID, subject: str, principal: Principal) -> None:
    if not principal.admin:
        raise ForbiddenError("only an administrator can assign ownership")
    if not subject.strip() or len(subject) > 200:
        raise ValueError("invalid subject")
    with conn.transaction():
        row = conn.execute(
            "UPDATE ingest_runs SET owner_subject = %s WHERE id = %s RETURNING id",
            (subject.strip(), run_id),
        ).fetchone()
        if row is None:
            raise NotFoundError("run not found")
        append_event(
            conn, "access", "completed", AuditDetails(operation="set_owner"), run_id=run_id
        )


def readers(conn: psycopg.Connection, run_id: UUID, principal: Principal) -> dict[str, object]:
    require_manager(conn, run_id, principal)
    owner = conn.execute(
        "SELECT owner_subject FROM ingest_runs WHERE id = %s", (run_id,)
    ).fetchone()
    assert owner is not None  # noqa: S101 - checked above
    subjects = conn.execute(
        "SELECT subject FROM run_readers WHERE run_id = %s ORDER BY subject", (run_id,)
    ).fetchall()
    return {"owner": owner[0], "readers": [r[0] for r in subjects]}


def set_readers(
    conn: psycopg.Connection, run_id: UUID, subjects: list[str], principal: Principal
) -> None:
    if any(not s.strip() or len(s) > 200 for s in subjects) or len(subjects) > 1000:
        raise ValueError("invalid reader subjects")
    with conn.transaction():
        conn.execute("SELECT 1 FROM ingest_runs WHERE id = %s FOR UPDATE", (run_id,))
        require_manager(conn, run_id, principal)
        append_event(
            conn,
            "access",
            "completed",
            AuditDetails(operation="set_readers", count=len(set(subjects))),
            run_id=run_id,
        )
        conn.execute("DELETE FROM run_readers WHERE run_id = %s", (run_id,))
        with conn.cursor() as cursor:
            cursor.executemany(
                "INSERT INTO run_readers (run_id, subject) VALUES (%s,%s)",
                [(run_id, s) for s in sorted({s.strip() for s in subjects})],
            )
