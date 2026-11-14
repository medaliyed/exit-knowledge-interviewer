"""Durable delivery metadata; payloads remain in the protected transcript only."""

from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from eki.audit import AuditDetails, append_event
from eki.errors import NotFoundError
from eki.interview import store
from eki.interview.models import Turn

Status = Literal["saved", "processing", "needs_retry", "processed"]


@dataclass(frozen=True)
class Receipt:
    id: UUID
    turn: Turn
    status: Status
    initial: dict[str, Any] | None
    result: dict[str, Any] | None

    def public(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "turn": self.turn.ordinal,
            "status": self.status,
            "question_turn": self.turn.answers,
            "corrects": self.turn.corrects,
            "result": self.result,
        }


def load(conn: psycopg.Connection, session_id: UUID, message_id: UUID) -> Receipt:
    row = conn.execute(
        "SELECT turn_ordinal, status, initial_event, result FROM message_receipts "
        "WHERE session_id = %s AND id = %s",
        (session_id, message_id),
    ).fetchone()
    if row is None:
        raise NotFoundError("message receipt not found")
    turn = store.load_turn(conn, session_id, row[0])
    assert turn is not None  # noqa: S101 - foreign key
    return Receipt(message_id, turn, row[1], row[2], row[3])


def pending(conn: psycopg.Connection, session_id: UUID) -> list[Receipt]:
    return [
        load(conn, session_id, row[0])
        for row in conn.execute(
            "SELECT id FROM message_receipts WHERE session_id = %s AND status <> 'processed' "
            "ORDER BY created_at, id",
            (session_id,),
        ).fetchall()
    ]


def audit(conn: psycopg.Connection, run_id: UUID, mid: UUID, status: Status) -> None:
    append_event(
        conn,
        "delivery",
        "completed" if status == "processed" else "started",
        AuditDetails(operation="deliver_message", target_id=mid, delivery_status=status),
        run_id=run_id,
    )


def save(
    conn: psycopg.Connection,
    session_id: UUID,
    run_id: UUID,
    mid: UUID,
    ordinal: int,
    initial: dict[str, Any] | None,
) -> Receipt:
    conn.execute(
        "INSERT INTO message_receipts (session_id, id, turn_ordinal, status, initial_event) "
        "VALUES (%s,%s,%s,'saved',%s)",
        (session_id, mid, ordinal, Jsonb(initial)),
    )
    audit(conn, run_id, mid, "saved")
    return load(conn, session_id, mid)


def transition(
    conn: psycopg.Connection,
    session_id: UUID,
    run_id: UUID,
    mid: UUID,
    status: Status,
    result: dict[str, Any] | None = None,
) -> None:
    with conn.transaction():
        conn.execute(
            "UPDATE message_receipts SET status = %s, result = %s, updated_at = now() "
            "WHERE session_id = %s AND id = %s",
            (status, None if result is None else Jsonb(result), session_id, mid),
        )
        audit(conn, run_id, mid, status)
