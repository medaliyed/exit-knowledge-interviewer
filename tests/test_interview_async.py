"""F7/F14: database latency and cleanup must not stall the async interview service."""

import asyncio
import threading
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import suppress
from typing import Any, cast
from uuid import UUID, uuid4

import psycopg
import pytest

from eki.async_db import db
from eki.audit import audit_context, current_context
from eki.auth import Principal, acting_as, current_principal
from eki.graph.workflow import Deps
from eki.ingest.embedding import HashingEmbedder
from eki.interview import store
from eki.interview.models import InterviewPlan
from eki.interview.service import Event, InterviewService, MessageStream
from tests.conftest import Scratch
from tests.fakes import ScriptedModel, tool_reply
from tests.interview_support import make_service, seed_run
from tests.test_interviewer import ask, note

pytestmark = pytest.mark.anyio


class Gate:
    """A blocking operation released by another coroutine, with a finite failure bound."""

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()

    def block(self) -> None:
        self.started.set()
        assert self.release.wait(2), "the event loop stalled while database work was blocked"

    async def entered(self) -> None:
        assert await asyncio.to_thread(self.started.wait, 2), "database work did not start"


class Connection:
    def __init__(self, close: Callable[[], None] = lambda: None) -> None:
        self.closed = False
        self.on_close = close

    def __enter__(self) -> "Connection":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def close(self) -> None:
        self.on_close()
        self.closed = True


def configured(
    monkeypatch: pytest.MonkeyPatch,
    conn: Connection,
    connect: Callable[[], psycopg.Connection] | None = None,
) -> tuple[InterviewService, InterviewPlan]:
    service = make_service("unused")
    plan = InterviewPlan(id=uuid4(), run_id=uuid4(), topics=())
    monkeypatch.setattr(service, "_connect", connect or (lambda: cast(psycopg.Connection, conn)))
    monkeypatch.setattr(store, "load_plan", lambda *_: plan)
    return service, plan


async def responsive(operation: Awaitable[object], gate: Gate) -> None:
    task = asyncio.ensure_future(operation)
    try:
        await gate.entered()
        assert not task.done(), "the event loop resumed only after database work finished"
    finally:
        gate.release.set()
        await task


@pytest.mark.parametrize("phase", ["connect", "close"])
async def test_slow_connection_lifecycle_keeps_the_event_loop_responsive(
    monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    gate = Gate()
    conn = Connection(gate.block if phase == "close" else lambda: None)

    def connect() -> psycopg.Connection:
        if phase == "connect":
            gate.block()
        return cast(psycopg.Connection, conn)

    service, plan = configured(monkeypatch, conn, connect)
    await responsive(service.get_plan(plan.id), gate)
    assert conn.closed


@pytest.mark.parametrize("phase", ["connect", "query", "query_error", "close"])
async def test_cancellation_finishes_database_work_before_releasing_the_connection(
    monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    gate = Gate()
    conn = Connection(gate.block if phase == "close" else lambda: None)

    def connect() -> psycopg.Connection:
        if phase == "connect":
            gate.block()
        return cast(psycopg.Connection, conn)

    service, plan = configured(monkeypatch, conn, connect)

    def query(*args: object) -> InterviewPlan:
        if phase in ("query", "query_error"):
            gate.block()
        if phase == "query_error":
            raise RuntimeError("worker failed after cancellation")
        assert not conn.closed, "connection was closed while its query was running"
        return plan

    monkeypatch.setattr(store, "load_plan", query)
    task = asyncio.create_task(service.get_plan(plan.id))
    try:
        await gate.entered()
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()  # A disconnect/shutdown may request cancellation more than once.
        await asyncio.sleep(0)
        assert not conn.closed
        assert not task.done(), "cancellation abandoned a worker that still owns the connection"
    finally:
        gate.release.set()
        with suppress(asyncio.CancelledError):
            await task
    assert conn.closed, "cancelled database work leaked its connection"


async def test_closing_an_unstarted_stream_does_not_block_the_event_loop() -> None:
    gate = Gate()
    conn = Connection(gate.block)

    async def unstarted() -> AsyncGenerator[Event]:
        raise AssertionError("closing an unstarted stream must not run the workflow")
        yield cast(Event, {})

    stream = MessageStream(unstarted(), cast(psycopg.Connection, conn))
    await responsive(stream.aclose(), gate)
    assert conn.closed


async def test_database_workers_keep_the_acting_identity_and_audit_context() -> None:
    principal = Principal("expert", "Synthetic Expert")
    run_id, correlation = uuid4(), uuid4()
    with (
        acting_as(principal),
        audit_context(user_id=principal.subject, run_ids=(run_id,), correlation_id=correlation),
    ):
        identity, context = await db(lambda: (current_principal(), current_context()))
    assert identity == principal
    assert context.user_id == principal.subject
    assert context.run_ids == (run_id,)
    assert context.correlation_id == correlation


@pytest.mark.db
async def test_dependency_queries_stay_off_loop_through_drafting_approval_and_recovery(
    scratch: Scratch, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = make_service(scratch.dsn)
    loop_thread = threading.get_ident()
    gate = Gate()
    original = service._deps

    def deps(
        conn: psycopg.Connection,
        *,
        run_id: UUID | None = None,
        plan_id: UUID | None = None,
        session_id: UUID | None = None,
    ) -> Deps:
        assert threading.get_ident() != loop_thread, "dependency queries ran on the event loop"
        return original(conn, run_id=run_id, plan_id=plan_id, session_id=session_id)

    def resolve(conn: psycopg.Connection, run_id: UUID) -> HashingEmbedder:
        gate.block()
        return HashingEmbedder()

    monkeypatch.setattr(service, "_deps", deps)
    service.artifact_resolver = resolve

    def seed() -> UUID:
        with scratch.connect() as conn:
            return seed_run(conn)

    run_id = await db(seed)

    async def journey() -> None:
        plan = await service.draft_plan(run_id)
        view = await service.approve(plan.id, "Nadia Ferrand", expected_revision=plan.revision)
        first = await service.post(view.session.id, text="First synthetic answer.")
        assert [event async for event in first][-1]["type"] == "question"
        saved = await service.post(view.session.id, text="Second synthetic answer.")
        await saved.aclose()
        recovered = await service.resume(view.session.id, saved.message_id)
        assert [event async for event in recovered][-1]["type"] == "question"

    await responsive(journey(), gate)


@pytest.mark.db
async def test_cancelled_stream_keeps_its_connection_until_the_tool_write_finishes(
    scratch: Scratch, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = ScriptedModel(
        [
            tool_reply(ask("Trigger?")),
            tool_reply(note("trigger", "Recorded."), ask("Steps?", "steps")),
        ]
    )
    service = make_service(scratch.dsn, agent=None, interviewer=model)
    with scratch.connect() as conn:
        run_id = seed_run(conn)
    plan = await service.draft_plan(run_id)
    view = await service.approve(plan.id, "Nadia Ferrand", expected_revision=plan.revision)
    stream = await service.post(
        view.session.id, text="Recorded.", message_id=uuid4(), question_turn=1
    )
    gate = Gate()
    original = store.record_note

    def paused(*args: Any, **kwargs: Any) -> None:
        gate.block()
        assert not stream.conn.closed, "disconnect closed a connection still used by a tool"
        original(*args, **kwargs)

    monkeypatch.setattr(store, "record_note", paused)

    async def consume() -> list[Event]:
        return [event async for event in stream]

    consumer = asyncio.create_task(consume())
    try:
        await gate.entered()
        consumer.cancel()
        await asyncio.sleep(0)
        assert not stream.conn.closed
        assert not consumer.done()
    finally:
        gate.release.set()
        with pytest.raises(asyncio.CancelledError):
            await consumer
    assert stream.conn.closed
    assert stream.message_id is not None
    assert (await service.receipt(view.session.id, stream.message_id))["status"] == "needs_retry"
    monkeypatch.setattr(store, "record_note", original)
    resumed = await service.resume(view.session.id, stream.message_id)
    assert [event async for event in resumed][-1]["turn"] == 3
    assert len(model.calls) == 2, "the stored reply recovers without another model call"
