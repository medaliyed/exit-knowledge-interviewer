"""Operations on plans and sessions, shared by the API and the command line.

Each operation opens its own database connection and builds the workflow over
the Postgres checkpointer, so nothing is kept in memory between requests: a
restarted process continues any session from its last question.
"""

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID, uuid4

import psycopg
from langgraph.types import Command

from eki.graph.workflow import (
    AgentNode,
    Deps,
    Limits,
    WorkflowError,
    db,
    forget_thread,
    open_workflow,
    thread_config,
)
from eki.ingest.embedding import Embedder
from eki.ingest.redaction import RedactedText, redact
from eki.interview import store
from eki.interview.interviewer import interview_topic
from eki.interview.models import (
    InterviewPlan,
    Note,
    OpenItem,
    SectionStatus,
    Session,
    Turn,
)
from eki.interview.plan import approve as approve_plan
from eki.llm import ChatModel, ModelError
from eki.runbook.models import SectionName

Event = dict[str, Any]
Action = Literal["answer", "skip", "correct"]


class NotFoundError(Exception):
    """No such run, plan, session or turn."""


class ConflictError(Exception):
    """The request does not fit the current state, for example an edit to an approved plan."""


class BusyError(ConflictError):
    """Someone else is working on this session right now."""


@dataclass(frozen=True)
class SessionView:
    session: Session
    waiting: Turn | None
    """The question the expert is being asked, if any."""
    checklist: dict[SectionName, SectionStatus]
    """Sections of the current topic. Empty when no topic is in progress."""
    turns: tuple[Turn, ...]
    notes: tuple[Note, ...]
    open_items: tuple[OpenItem, ...]


@dataclass
class InterviewService:
    dsn: str
    drafter: ChatModel
    interviewer: ChatModel
    embedder: Embedder
    work_email_domains: frozenset[str]
    agent: AgentNode = interview_topic
    limits: Limits = field(default_factory=Limits)
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)

    def _connect(self) -> psycopg.Connection:
        return psycopg.connect(self.dsn, autocommit=True)

    def _deps(self, conn: psycopg.Connection) -> Deps:
        return Deps(
            conn=conn,
            drafter=self.drafter,
            interviewer=self.interviewer,
            embedder=self.embedder,
            work_email_domains=self.work_email_domains,
            limits=self.limits,
        )

    # --- plans ---

    async def draft_plan(self, run_id: UUID) -> InterviewPlan:
        """Draft a plan for a mapped run and stop for the expert's review."""
        plan_id = uuid4()
        with self._connect() as conn:
            if not await db(store.run_exists, conn, run_id):
                raise NotFoundError(f"no completed run {run_id}")
            try:
                async with open_workflow(self.dsn, self._deps(conn), self.agent) as workflow:
                    await workflow.ainvoke(
                        {"run_id": str(run_id), "plan_id": str(plan_id)},
                        thread_config(plan_id),
                        durability="sync",
                    )
            except WorkflowError as error:
                await forget_thread(self.dsn, plan_id)
                raise ConflictError(str(error)) from error
            except ModelError:
                await forget_thread(self.dsn, plan_id)
                raise
            plan = await db(store.load_plan, conn, plan_id)
        assert plan is not None  # noqa: S101 - the workflow just saved it
        return plan

    async def get_plan(self, plan_id: UUID) -> InterviewPlan:
        with self._connect() as conn:
            plan = await db(store.load_plan, conn, plan_id)
        if plan is None:
            raise NotFoundError(f"no plan {plan_id}")
        return plan

    async def edit_plan(
        self, plan_id: UUID, edit: Callable[[InterviewPlan], InterviewPlan]
    ) -> InterviewPlan:
        """Apply an edit from ``eki.interview.plan``. Raises ``PlanFrozenError`` once approved."""

        def work() -> InterviewPlan:
            with self._connect() as conn, conn.transaction():
                conn.execute("SELECT 1 FROM interview_plans WHERE id = %s FOR UPDATE", (plan_id,))
                plan = store.load_plan(conn, plan_id)
                if plan is None:
                    raise NotFoundError(f"no plan {plan_id}")
                changed = edit(plan)
                store.save_plan(conn, changed)
                return changed

        return await db(work)

    async def approve(self, plan_id: UUID, by: str) -> SessionView:
        """Freeze the plan, open its session and ask the first question."""
        with self._connect() as conn:
            if not await db(store.try_lock, conn, plan_id):
                raise BusyError("the plan is being changed by someone else")
            plan = await db(store.load_plan, conn, plan_id)
            if plan is None:
                raise NotFoundError(f"no plan {plan_id}")
            if plan.status == "draft":
                await db(store.save_plan, conn, approve_plan(plan, by, self.clock()))
            elif await db(store.session_of_plan, conn, plan_id) is not None:
                raise ConflictError("the plan is already approved")
            async with open_workflow(self.dsn, self._deps(conn), self.agent) as workflow:
                await workflow.ainvoke(
                    Command(resume={"session_id": str(uuid4())}),
                    thread_config(plan_id),
                    durability="sync",
                )
            session_id = await db(store.session_of_plan, conn, plan_id)
        assert session_id is not None  # noqa: S101 - the workflow just opened it
        return await self.view(session_id)

    # --- sessions ---

    async def view(self, session_id: UUID) -> SessionView:
        def work() -> SessionView:
            with self._connect() as conn:
                session = store.load_session(conn, session_id)
                if session is None:
                    raise NotFoundError(f"no session {session_id}")
                topic_id = session.current_topic_id
                return SessionView(
                    session=session,
                    waiting=(
                        store.waiting_question(conn, session_id)
                        if session.status == "active"
                        else None
                    ),
                    checklist=store.checklist(conn, session_id, topic_id) if topic_id else {},
                    turns=store.load_turns(conn, session_id),
                    notes=store.load_notes(conn, session_id),
                    open_items=store.load_open_items(conn, session_id),
                )

        return await db(work)

    async def post(
        self,
        session_id: UUID,
        action: Action = "answer",
        text: str = "",
        *,
        confidential: bool = False,
        corrects: int | None = None,
    ) -> AsyncIterator[Event]:
        """Take the expert's message and return the events of the interviewer's next step.

        Everything that can be refused is refused here, before the first event.
        The returned stream always ends with ``question``, ``done`` or ``error``.
        """
        conn = self._connect()
        try:
            session = await db(store.load_session, conn, session_id)
            if session is None:
                raise NotFoundError(f"no session {session_id}")
            if not await db(store.try_lock, conn, session.plan_id):
                raise BusyError("another message for this session is being handled")
            command = await self._accept(conn, session, action, text, confidential, corrects)
        except BaseException:
            conn.close()
            raise
        return self._stream(conn, session, command)

    async def _pending(self, conn: psycopg.Connection, session: Session) -> dict[str, Any] | None:
        """The question the workflow is stopped at, from its checkpoint."""
        async with open_workflow(self.dsn, self._deps(conn), self.agent) as workflow:
            snapshot = await workflow.aget_state(thread_config(session.plan_id))
        for waiting in snapshot.interrupts:
            if isinstance(waiting.value, dict) and waiting.value.get("type") == "question":
                return waiting.value
        return None

    async def _accept(
        self,
        conn: psycopg.Connection,
        session: Session,
        action: Action,
        text: str,
        confidential: bool,
        corrects: int | None,
    ) -> Command[Any] | None:
        """Validate and store the expert's message. Returns how to continue the workflow."""
        if session.status == "finished":
            raise ConflictError("the interview is finished")
        pending = await self._pending(conn, session)
        if pending is None:
            # The process stopped mid-step. Continue it; the message is not used.
            return None
        if action != "answer":
            raise ValueError(f"unsupported action: {action}")
        if not text.strip():
            raise ValueError("an answer needs text")
        answer: RedactedText = redact(text, work_email_domains=self.work_email_domains).text
        turn = await db(
            store.add_expert_turn,
            conn,
            session.id,
            pending.get("topic_id"),
            "answer",
            answer,
            answers=int(pending["turn"]),
        )
        return Command(resume={"turn": turn})

    async def _stream(
        self, conn: psycopg.Connection, session: Session, command: Command[Any] | None
    ) -> AsyncIterator[Event]:
        config = thread_config(session.plan_id)
        try:
            if command is None:
                yield {
                    "type": "recovered",
                    "message": "The session had stopped mid-step. Your message was not used.",
                }
            async with open_workflow(self.dsn, self._deps(conn), self.agent) as workflow:
                try:
                    async for mode, chunk in workflow.astream(
                        command, config, stream_mode=["custom"], durability="sync"
                    ):
                        if mode == "custom" and isinstance(chunk, dict):
                            yield chunk
                except ModelError:
                    yield {
                        "type": "error",
                        "code": "model_unavailable",
                        "message": "The model could not be reached. Nothing was lost: "
                        "send the same message again.",
                    }
                    return
                snapshot = await workflow.aget_state(config)
            waiting = next(
                (
                    w.value
                    for w in snapshot.interrupts
                    if isinstance(w.value, dict) and w.value.get("type") == "question"
                ),
                None,
            )
            yield {**waiting} if waiting is not None else {"type": "done"}
        finally:
            conn.close()
