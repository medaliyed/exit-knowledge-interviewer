"""Durable metadata-only audit, with context propagated across async tasks and threads."""

import asyncio
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Literal
from uuid import UUID, uuid4

import psycopg
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, field_validator

from eki.ingest.redaction import redact
from eki.llm import ChatModel, Message, Reply, ToolSpec

Operation = Literal[
    "deliver_message",
    "generate_plan",
    "draft",
    "interview",
    "answer",
    "embedding",
    "approve_plan",
    "approve",
    "reject",
    "edit",
    "correction",
    "set_owner",
    "set_readers",
    "ingest",
    "search_artifacts",
    "get_artifact",
    "ask_expert",
    "record_note",
    "mark_open",
    "finish_topic",
    "unknown_tool",
    "list_commits",
    "list_pull_requests",
    "list_documents",
    "get_repo_activity",
    "list_tickets",
    "get_ticket_activity",
    "get_path_activity",
    "list_messages",
    "get_message",
    "handover",
    "review",
    "withdraw",
    "flag_created",
    "flag_resolved",
    "follow_up_requested",
    "gap_triaged",
    "follow_up_prepared",
    "follow_up_finished",
    "follow_up_discarded",
    "follow_up_proposed",
    "follow_up_refreshed",
    "follow_up_apply",
    "gap_resolved",
    "sign_in",
    "sign_out",
    "session_expired",
    "password_set",
    "password_changed",
    "link_created",
    "account_created",
    "account_updated",
    "account_disabled",
    "account_enabled",
    "sessions_revoked",
]
Kind = Literal[
    "model", "tool", "approval", "review", "access", "ingest", "delivery", "generation", "auth"
]
Phase = Literal["started", "completed", "failed", "refused", "skipped"]


class AuditError(Exception):
    """Audit storage is unavailable; refuse unaudited operations without leaking a DSN."""


class AuditDetails(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    operation: Operation
    generation_status: Literal["running", "needs_retry", "completed"] | None = None
    delivery_status: Literal["saved", "processing", "needs_retry", "processed"] | None = None
    model_name: str | None = Field(default=None, max_length=200)
    target_id: UUID | None = None
    version: int | None = Field(default=None, ge=1)
    count: int | None = Field(default=None, ge=0)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    error_type: (
        Literal["Error", "ModelError", "ValueError", "PermissionError", "Cancelled"] | None
    ) = None

    @field_validator("model_name", mode="before")
    @classmethod
    def clean_model(cls, value: str | None) -> str | None:
        return (
            None if value is None else str(redact(value, work_email_domains=frozenset()).text)[:200]
        )


@dataclass(frozen=True)
class AuditContext:
    user_id: str = "local-operator"
    run_ids: tuple[UUID, ...] = ()
    correlation_id: UUID = field(default_factory=uuid4)
    recorder: "AuditRecorder | None" = None


_context: ContextVar[AuditContext | None] = ContextVar("eki_audit", default=None)


def current_context() -> AuditContext:
    value = _context.get()
    if value is None:
        value = AuditContext()
        _context.set(value)
    return value


@contextmanager
def audit_context(
    *,
    user_id: str | None = None,
    run_ids: tuple[UUID, ...] | None = None,
    correlation_id: UUID | None = None,
    recorder: "AuditRecorder | None" = None,
) -> Iterator[AuditContext]:
    previous = current_context()
    value = AuditContext(
        user_id or previous.user_id,
        previous.run_ids if run_ids is None else run_ids,
        correlation_id or previous.correlation_id,
        recorder or previous.recorder,
    )
    token = _context.set(value)
    try:
        yield value
    finally:
        _context.reset(token)


def append_event(
    conn: psycopg.Connection,
    kind: Kind,
    phase: Phase,
    details: AuditDetails,
    *,
    run_id: UUID | None = None,
    call_id: UUID | None = None,
) -> None:
    context = current_context()
    scope = run_id or next(iter(context.run_ids), None)
    details = AuditDetails.model_validate(details.model_dump())
    try:
        with conn.transaction():
            # Serializing inserts until commit makes numeric cursors commit-ordered.
            # Audit writes are the final DB step of state-changing operations.
            conn.execute(
                "SELECT pg_advisory_xact_lock("
                "hashtextextended(current_schema() || '.audit_events', 0))"
            )
            conn.execute(
                "INSERT INTO audit_events "
                "(event_id, correlation_id, call_id, run_id, user_id, kind, phase, details) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    uuid4(),
                    context.correlation_id,
                    call_id,
                    scope,
                    context.user_id,
                    kind,
                    phase,
                    Jsonb(details.model_dump(mode="json", exclude_none=True)),
                ),
            )
    except psycopg.Error:
        raise AuditError("audit storage unavailable; operation refused") from None


def list_events(
    conn: psycopg.Connection,
    run_id: UUID,
    *,
    after: int = 0,
    limit: int = 100,
) -> list[dict[str, object]]:
    if after < 0 or not 1 <= limit <= 500:
        raise ValueError("audit pagination requires after >= 0 and limit between 1 and 500")
    rows = conn.execute(
        "SELECT id, event_id, at, correlation_id, call_id, run_id, user_id, kind, phase, details "
        "FROM audit_events WHERE run_id = %s AND id > %s ORDER BY id LIMIT %s",
        (run_id, after, limit),
    ).fetchall()
    return [
        {
            "id": r[0],
            "event_id": str(r[1]),
            "at": r[2].isoformat(),
            "correlation_id": str(r[3]),
            "call_id": None if r[4] is None else str(r[4]),
            "run_id": str(r[5]),
            "user_id": r[6],
            "kind": r[7],
            "phase": r[8],
            "details": r[9],
        }
        for r in rows
    ]


def list_auth_events(
    conn: psycopg.Connection, *, after: int = 0, limit: int = 100
) -> list[dict[str, object]]:
    """Run-less authentication history, for the trusted local operator only."""
    if after < 0 or not 1 <= limit <= 500:
        raise ValueError("audit pagination requires after >= 0 and limit between 1 and 500")
    rows = conn.execute(
        "SELECT id, event_id, at, correlation_id, call_id, run_id, user_id, kind, phase, details "
        "FROM audit_events WHERE kind = 'auth' AND run_id IS NULL AND id > %s ORDER BY id LIMIT %s",
        (after, limit),
    ).fetchall()
    return [
        {
            "id": row[0],
            "event_id": str(row[1]),
            "at": row[2].isoformat(),
            "correlation_id": str(row[3]),
            "call_id": None if row[4] is None else str(row[4]),
            "run_id": None,
            "user_id": row[6],
            "kind": row[7],
            "phase": row[8],
            "details": row[9],
        }
        for row in rows
    ]


@dataclass(repr=False)
class AuditRecorder:
    dsn: str

    def record(
        self,
        kind: Kind,
        phase: Phase,
        details: AuditDetails,
        *,
        call_id: UUID,
        run_ids: tuple[UUID, ...] | None = None,
    ) -> None:
        ids = current_context().run_ids if run_ids is None else run_ids
        try:
            with psycopg.connect(self.dsn, autocommit=True) as conn, conn.transaction():
                for run_id in tuple(dict.fromkeys(ids)) or (None,):
                    append_event(conn, kind, phase, details, run_id=run_id, call_id=call_id)
        except psycopg.Error:
            raise AuditError("audit storage unavailable; operation refused") from None


class AuditedModel:
    def __init__(
        self,
        inner: ChatModel,
        recorder: AuditRecorder,
        operation: Operation,
        run_ids: tuple[UUID, ...],
    ) -> None:
        self.inner, self.recorder, self.operation, self.run_ids = (
            inner,
            recorder,
            operation,
            run_ids,
        )

    @property
    def name(self) -> str:
        return self.inner.name

    async def complete(self, messages: Sequence[Message], tools: Sequence[ToolSpec] = ()) -> Reply:
        current_context()
        call_id = uuid4()
        details = AuditDetails(operation=self.operation, model_name=self.name)
        await asyncio.to_thread(
            self.recorder.record, "model", "started", details, call_id=call_id, run_ids=self.run_ids
        )
        try:
            reply = await self.inner.complete(messages, tools)
        except BaseException as error:
            failed = details.model_copy(
                update={"error_type": "Cancelled" if not isinstance(error, Exception) else "Error"}
            )
            await asyncio.to_thread(
                self.recorder.record,
                "model",
                "failed",
                failed,
                call_id=call_id,
                run_ids=self.run_ids,
            )
            raise
        completed = details.model_copy(
            update={
                "input_tokens": max(0, reply.input_tokens),
                "output_tokens": max(0, reply.output_tokens),
            }
        )
        await asyncio.to_thread(
            self.recorder.record,
            "model",
            "completed",
            completed,
            call_id=call_id,
            run_ids=self.run_ids,
        )
        return reply
