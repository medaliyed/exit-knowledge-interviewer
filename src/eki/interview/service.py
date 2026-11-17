"""Operations on plans and sessions, shared by the API and the command line.

Each operation opens its own database connection and builds the workflow over
the Postgres checkpointer, so nothing is kept in memory between requests: a
restarted process continues any session from its last question.
"""

from collections.abc import AsyncGenerator, AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID, uuid4

import psycopg
from langgraph.types import Command

from eki import maintenance
from eki.audit import AuditedModel, AuditError, AuditRecorder, Operation
from eki.auth import current_principal
from eki.config import Settings, model_name, work_domains
from eki.errors import ConflictError as ConflictError
from eki.errors import NotFoundError as NotFoundError
from eki.followup import sessions as follow_ups
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
from eki.ingest.embedding import Embedder, HashingEmbedder
from eki.ingest.redaction import RedactedText, redact
from eki.ingest.semantic import for_run as artifact_embedder_for_run
from eki.interview import delivery, generation, store
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
from eki.llm.litellm_model import LiteLLMModel
from eki.llm.tracing import NullTracer, TracedModel, Tracer, build_tracer
from eki.runbook.models import SectionName
from eki.runbook.service import correct_entries
from eki.search.embedding import AuditedEmbedder

Event = dict[str, Any]
Action = Literal["answer", "skip", "correct"]


class BusyError(ConflictError):
    """Someone else is working on this session right now."""


class GenerationFailed(ModelError):
    def __init__(self, generation_id: UUID) -> None:
        self.generation_id = generation_id
        super().__init__(f"model unavailable; saved plan generation {generation_id} can be resumed")


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
    scope: tuple[SectionName, ...] = ()
    """Sections in scope for the current topic; a follow-up may select fewer than seven."""
    usage: dict[str, Any] | None = None
    """Follow-up sessions: question/model/embedding counts, frozen limits and completion."""


@dataclass
class InterviewService:
    dsn: str = field(repr=False)
    drafter: ChatModel
    interviewer: ChatModel
    embedder: Embedder
    work_email_domains: frozenset[str]
    agent: AgentNode = interview_topic
    limits: Limits = field(default_factory=Limits)
    tracer: Tracer = field(default_factory=NullTracer)
    artifact_resolver: Callable[[psycopg.Connection, UUID], Embedder] | None = None
    draft_configuration: dict[str, Any] = field(default_factory=dict)
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)

    def _connect(self) -> psycopg.Connection:
        return psycopg.connect(self.dsn, autocommit=True)

    async def _open(self) -> psycopg.Connection:
        """Open off-loop; cancellation must close even a connection created after it arrives."""
        conn: psycopg.Connection | None = None

        def connect() -> psycopg.Connection:
            nonlocal conn
            conn = self._connect()
            return conn

        try:
            return await db(connect)
        except BaseException:
            if conn is not None:
                await db(conn.close)
            raise

    @asynccontextmanager
    async def _connection(self) -> AsyncIterator[psycopg.Connection]:
        """Own one connection across async steps, including off-loop failure cleanup."""
        conn = await self._open()
        try:
            yield conn
        finally:
            await db(conn.close)

    def _deps(
        self,
        conn: psycopg.Connection,
        *,
        run_id: UUID | None = None,
        plan_id: UUID | None = None,
        session_id: UUID | None = None,
    ) -> Deps:
        """Build in a database worker: resolving budgets and artifact models can query Postgres."""
        recorder = AuditRecorder(self.dsn)
        if session_id is not None:
            session = store.load_session(conn, session_id)
            plan = store.load_plan(conn, session.plan_id) if session is not None else None
            if plan is not None and plan.kind == "follow_up" and plan.limits is not None:
                recorder = follow_ups.FollowUpRecorder(self.dsn, session_id, plan.limits)

        def traced(model: ChatModel, step: Operation) -> ChatModel:
            return TracedModel(
                AuditedModel(model, recorder, step, () if run_id is None else (run_id,)),
                self.tracer,
                step=step,
                work_email_domains=self.work_email_domains,
                run_id=None if run_id is None else str(run_id),
                plan_id=None if plan_id is None else str(plan_id),
                session_id=None if session_id is None else str(session_id),
            )

        embedder = (
            self.artifact_resolver(conn, run_id)
            if self.artifact_resolver is not None and run_id is not None
            else self.embedder
        )
        if isinstance(recorder, follow_ups.FollowUpRecorder) and not isinstance(
            embedder, HashingEmbedder
        ):
            inner = embedder.inner if isinstance(embedder, AuditedEmbedder) else embedder
            embedder = AuditedEmbedder(inner, recorder, () if run_id is None else (run_id,))
        return Deps(
            conn=conn,
            audit=AuditRecorder(self.dsn),
            drafter=traced(self.drafter, "draft"),
            interviewer=traced(self.interviewer, "interview"),
            embedder=embedder,
            work_email_domains=self.work_email_domains,
            limits=self.limits,
            draft_configuration=self.draft_configuration,
        )

    # --- plans ---

    async def generation_status(self, run_id: UUID, generation_id: UUID) -> dict[str, Any]:
        async with self._connection() as conn:
            return await db(generation.read, conn, run_id, generation_id)

    async def draft_plan(self, run_id: UUID, *, generation_id: UUID | None = None) -> InterviewPlan:
        """Start a new generation or resume a known one, retaining validated topic results."""
        plan_id = generation_id or uuid4()
        async with self._connection() as conn:
            if not await db(store.run_exists, conn, run_id):
                raise NotFoundError(f"no completed run {run_id}")
            await db(maintenance.capture, conn, run_id)
            owner = await db(generation.owner_run, conn, plan_id)
            existing = await db(store.load_plan, conn, plan_id)
            if (owner is not None and owner != run_id) or (
                existing is not None and existing.run_id != run_id
            ):
                raise NotFoundError("plan generation not found")
            if not await db(store.try_lock, conn, plan_id):
                raise BusyError("this plan generation is being handled")
            if existing is not None and existing.status == "approved":
                return existing
            try:
                deps = await db(self._deps, conn, run_id=run_id, plan_id=plan_id)
                async with open_workflow(self.dsn, deps, self.agent) as workflow:
                    config = thread_config(plan_id)
                    snapshot = await workflow.aget_state(config)
                    if snapshot.values and snapshot.values.get("run_id") != str(run_id):
                        raise NotFoundError("plan generation not found")
                    if existing is not None and any(
                        isinstance(i.value, dict) and i.value.get("type") == "plan_review"
                        for i in snapshot.interrupts
                    ):
                        return existing
                    await workflow.ainvoke(
                        None
                        if snapshot.values
                        else {"run_id": str(run_id), "plan_id": str(plan_id)},
                        config,
                        durability="sync",
                    )
            except WorkflowError as error:
                if await db(generation.owner_run, conn, plan_id) is None:
                    await forget_thread(self.dsn, plan_id)
                raise ConflictError(str(error)) from error
            except ModelError as error:
                await db(generation.mark_retry, conn, run_id, plan_id)
                raise GenerationFailed(plan_id) from error
            except BaseException:
                # Keep topic results/checkpoints even if failure reporting is unavailable.
                with suppress(AuditError, psycopg.Error):
                    await db(generation.mark_retry, conn, run_id, plan_id)
                raise
            plan = await db(store.load_plan, conn, plan_id)
        assert plan is not None  # noqa: S101 - workflow publication is atomic
        return plan

    async def get_plan(self, plan_id: UUID) -> InterviewPlan:
        async with self._connection() as conn:
            plan = await db(store.load_plan, conn, plan_id)
        if plan is None:
            raise NotFoundError(f"no plan {plan_id}")
        return plan

    async def session_for_plan(self, plan_id: UUID) -> UUID | None:
        """The session opened by approving this plan, if it has been approved."""
        async with self._connection() as conn:
            return await db(store.session_of_plan, conn, plan_id)

    async def edit_plan(
        self,
        plan_id: UUID,
        edit: Callable[[InterviewPlan], InterviewPlan],
        *,
        expected_revision: int,
    ) -> InterviewPlan:
        """Apply an edit from ``eki.interview.plan``. Raises ``PlanFrozenError`` once approved."""

        def work() -> InterviewPlan:
            with self._connect() as conn, conn.transaction():
                row = conn.execute(
                    "SELECT run_id FROM interview_plans WHERE id=%s", (plan_id,)
                ).fetchone()
                if row is None:
                    raise NotFoundError("plan not found")
                maintenance.capture(conn, row[0])
                maintenance.lock_run(conn, row[0])
                conn.execute("SELECT 1 FROM interview_plans WHERE id = %s FOR UPDATE", (plan_id,))
                plan = store.load_plan(conn, plan_id)
                if plan is None:
                    raise NotFoundError(f"no plan {plan_id}")
                if plan.revision != expected_revision:
                    raise ConflictError("plan changed; refresh before editing")
                changed = edit(plan)
                follow_ups.validate_edit(plan, changed)
                if plan.kind == "follow_up":
                    topics = []
                    for topic in changed.topics:
                        questions = tuple(
                            q.model_copy(
                                update={
                                    "text": redact(
                                        q.text,
                                        work_email_domains=self.work_email_domains,
                                    ).text
                                }
                            )
                            for q in topic.questions
                        )
                        topics.append(topic.model_copy(update={"questions": questions}))
                    changed = changed.model_copy(update={"topics": tuple(topics)})
                store.save_plan(conn, changed)
                follow_ups.release(conn, plan, changed)
                return store.load_plan(conn, plan_id) or changed

        return await db(work)

    async def approve(self, plan_id: UUID, by: str, *, expected_revision: int) -> SessionView:
        """Freeze the plan, open its session and ask the first question."""
        async with self._connection() as conn:
            plan_run = await db(
                lambda: conn.execute(
                    "SELECT run_id FROM interview_plans WHERE id=%s", (plan_id,)
                ).fetchone()
            )
            if plan_run is None:
                raise NotFoundError("plan not found")
            await db(maintenance.capture, conn, plan_run[0])
            if not await db(store.try_lock, conn, plan_id):
                raise BusyError("the plan is being changed by someone else")
            plan = await db(store.load_plan, conn, plan_id)
            if plan is None:
                raise NotFoundError(f"no plan {plan_id}")
            follow_up = plan.kind == "follow_up"
            respondent = None
            if follow_up:
                respondent = await db(
                    follow_ups.respondent, conn, plan.run_id, current_principal(), by
                )
            existing_session = await db(store.session_of_plan, conn, plan_id)
            if plan.status == "draft":
                if expected_revision != plan.revision:
                    raise ConflictError("plan changed; refresh before approving")
                if follow_up:
                    await db(follow_ups.validate_selection, conn, plan)
                await db(store.save_plan, conn, approve_plan(plan, by, self.clock()))
            elif existing_session is not None:
                if follow_up:
                    previous = await self.view(existing_session)
                    if previous.session.status == "finished" or previous.waiting is not None:
                        return previous  # completed approval retries spend no provider allowance
                else:
                    raise ConflictError("the plan is already approved")
            new_session = existing_session or uuid4()
            # A durable session also identifies the budget recorder before its first call.
            if follow_up:
                new_session = await db(store.create_session, conn, new_session, plan.id, respondent)
            deps = await db(
                self._deps, conn, run_id=plan.run_id, plan_id=plan_id, session_id=new_session
            )
            async with open_workflow(self.dsn, deps, self.agent) as workflow:
                config = thread_config(plan_id)
                if follow_up and not (await workflow.aget_state(config)).values:
                    # Prepared without the drafting node: start the thread up to plan review.
                    await workflow.ainvoke(
                        {"run_id": str(plan.run_id), "plan_id": str(plan_id)},
                        config,
                        durability="sync",
                    )
                snapshot = await workflow.aget_state(config)
                command: Command[Any] | None = (
                    Command(resume={"session_id": str(new_session), "respondent": respondent})
                    if snapshot.interrupts
                    else None
                )
                await workflow.ainvoke(
                    command,
                    config,
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
                in_scope = store.scope(conn, session_id, topic_id) if topic_id else ()
                return SessionView(
                    session=session,
                    waiting=(
                        store.waiting_question(conn, session_id)
                        if session.status == "active"
                        else None
                    ),
                    checklist=(
                        {
                            s: v
                            for s, v in store.checklist(conn, session_id, topic_id).items()
                            if s in in_scope
                        }
                        if topic_id
                        else {}
                    ),
                    turns=store.load_turns(conn, session_id),
                    notes=store.load_notes(conn, session_id),
                    open_items=store.load_open_items(conn, session_id),
                    scope=in_scope,
                    usage=(
                        follow_ups.usage(conn, session_id) if session.kind == "follow_up" else None
                    ),
                )

        return await db(work)

    async def receipt(self, session_id: UUID, message_id: UUID) -> dict[str, Any]:
        async with self._connection() as conn:
            session = await db(store.load_session, conn, session_id)
            if session is None:
                raise NotFoundError("session not found")
            found = await db(delivery.load, conn, session_id, message_id)
            if found.status == "processing" and await db(store.try_lock, conn, session.plan_id):
                found = await db(delivery.load, conn, session_id, message_id)
                if found.status == "processing":
                    await db(
                        delivery.transition,
                        conn,
                        session_id,
                        session.run_id,
                        message_id,
                        "needs_retry",
                    )
                    found = await db(delivery.load, conn, session_id, message_id)
            return found.public()

    async def receipts(self, session_id: UUID) -> list[dict[str, Any]]:
        async with self._connection() as conn:
            ids = await db(lambda: [r.id for r in delivery.pending(conn, session_id)])
        return [await self.receipt(session_id, mid) for mid in ids]

    async def resume(self, session_id: UUID, message_id: UUID | None = None) -> "MessageStream":
        """Resume accepted work without asking for its text a second time."""
        conn, session = await self._locked(session_id)
        try:
            pending = await db(delivery.pending, conn, session_id)
            receipt = (
                await db(delivery.load, conn, session_id, message_id)
                if message_id is not None
                else next(iter(pending), None)
            )
            return MessageStream(
                self._stream(conn, session, receipt, modern=True),
                conn,
                None if receipt is None else receipt.id,
            )
        except BaseException:
            await db(conn.close)
            raise

    async def _locked(self, session_id: UUID) -> tuple[psycopg.Connection, Session]:
        conn = await self._open()
        try:
            session = await db(store.load_session, conn, session_id)
            if session is None:
                raise NotFoundError(f"no session {session_id}")
            await db(maintenance.capture, conn, session.run_id)
            if session.kind == "follow_up":
                # Only the recorded respondent answers or resumes a follow-up (F22 I2).
                subject = await db(follow_ups.respondent, conn, session.run_id, current_principal())
                if session.respondent_subject not in (None, subject):
                    raise NotFoundError(f"no session {session_id}")
            if not await db(store.try_lock, conn, session.plan_id):
                raise BusyError("another message for this session is being handled")
            return conn, session
        except BaseException:
            await db(conn.close)
            raise

    async def post(
        self,
        session_id: UUID,
        action: Action = "answer",
        text: str = "",
        *,
        confidential: bool = False,
        corrects: int | None = None,
        message_id: UUID | None = None,
        question_turn: int | None = None,
    ) -> "MessageStream":
        conn, session = await self._locked(session_id)
        try:
            modern = message_id is not None
            clean = (
                RedactedText("(skipped)")
                if action == "skip"
                else redact(text, work_email_domains=self.work_email_domains).text
            )
            if action not in ("answer", "skip", "correct") or not clean.strip():
                raise ValueError("an answer needs text and a valid action")
            pending = await db(delivery.pending, conn, session_id)
            existing = None
            if message_id is not None:
                with suppress(NotFoundError):
                    existing = await db(delivery.load, conn, session_id, message_id)
            elif pending:
                existing = pending[0]  # legacy retries must match the saved submission
            if existing is not None:
                t = existing.turn
                if (
                    t.kind != ("correction" if action == "correct" else action)
                    or t.text != clean
                    or t.corrects != corrects
                    or (question_turn is not None and t.answers != question_turn)
                    or (action == "answer" and t.confidential != confidential)
                ):
                    raise ConflictError("message differs from saved submission; resume it first")
                receipt = existing
            else:
                if pending:
                    raise ConflictError("resume the saved message before sending another")
                mid = message_id or uuid4()

                def accept() -> delivery.Receipt:
                    with conn.transaction():
                        initial = None
                        if action == "correct":
                            if question_turn is not None or confidential:
                                raise ValueError(
                                    "corrections use corrects, not question/confidential fields"
                                )
                            initial = self._correct(conn, session, text, corrects)
                            ordinal = int(initial["turn"])
                        else:
                            if corrects is not None:
                                raise ValueError("only corrections may specify corrects")
                            if session.status == "finished":
                                raise ConflictError("the interview is finished")
                            waiting = store.waiting_question(conn, session_id)
                            if waiting is None or (
                                question_turn is not None and waiting.ordinal != question_turn
                            ):
                                raise ConflictError(
                                    "question is not waiting; resume the session first"
                                )
                            ordinal = store.add_expert_turn(
                                conn,
                                session_id,
                                waiting.topic_id,
                                action,
                                clean,
                                answers=waiting.ordinal,
                                confidential=confidential and action == "answer",
                            )
                        return delivery.save(
                            conn, session_id, session.run_id, mid, ordinal, initial
                        )

                receipt = await db(accept)
            return MessageStream(
                self._stream(conn, session, receipt, modern=modern), conn, receipt.id
            )
        except BaseException:
            await db(conn.close)
            raise

    async def _pending(self, conn: psycopg.Connection, session: Session) -> dict[str, Any] | None:
        deps = await db(self._deps, conn)
        async with open_workflow(self.dsn, deps, self.agent) as workflow:
            snapshot = await workflow.aget_state(thread_config(session.plan_id))
        return next(
            (
                w.value
                for w in snapshot.interrupts
                if isinstance(w.value, dict) and w.value.get("type") == "question"
            ),
            None,
        )

    def _correct(
        self, conn: psycopg.Connection, session: Session, text: str, corrects: int | None
    ) -> Event:
        """Store a correction and replace the notes taken from the corrected turn (F8)."""
        if corrects is None:
            raise ValueError("a correction must say which turn it corrects")
        if not text.strip():
            raise ValueError("a correction needs text")
        target = store.load_turn(conn, session.id, corrects)
        if target is None:
            raise NotFoundError(f"no turn {corrects} in this session")
        if target.speaker != "expert" or target.kind == "skip":
            raise ValueError("only something the expert said can be corrected")
        clean = redact(text, work_email_domains=self.work_email_domains).text
        with conn.transaction():
            conn.execute("SELECT 1 FROM sessions WHERE id = %s FOR UPDATE", (session.id,))
            turn = store.add_expert_turn(
                conn,
                session.id,
                target.topic_id,
                "correction",
                clean,
                corrects=corrects,
                confidential=target.confidential,
            )
            changed = store.replace_notes_from_turn(conn, session.id, corrects, clean, turn)
            expert = conn.execute(
                "SELECT expert_name FROM ingest_runs WHERE id = %s", (session.run_id,)
            ).fetchone()
            assert expert is not None  # noqa: S101 - session references this run
            correct_entries(
                conn,
                session.id,
                corrects,
                turn,
                clean,
                redact(expert[0], work_email_domains=self.work_email_domains).text,
                self.clock(),
            )
        sections = [section for _, section in changed]
        return {"type": "corrected", "turn": turn, "corrects": corrects, "sections": sections}

    async def _stream(
        self,
        conn: psycopg.Connection,
        session: Session,
        receipt: delivery.Receipt | None,
        *,
        modern: bool,
    ) -> AsyncGenerator[Event]:
        mid = None if receipt is None else receipt.id
        complete = False
        try:
            if receipt is not None and receipt.status == "processed":
                complete = True
                if modern:
                    yield {"type": "delivery", **receipt.public()}
                if receipt.initial is not None:
                    yield receipt.initial
                yield receipt.result or {"type": "done"}
                complete = True
                return
            if receipt is not None:
                if modern:
                    yield {"type": "delivery", **receipt.public()}
                await db(
                    delivery.transition, conn, session.id, session.run_id, receipt.id, "processing"
                )
                if receipt.initial is not None:
                    yield receipt.initial
            config = thread_config(session.plan_id)
            pending = await self._pending(conn, session)
            correction = receipt is not None and receipt.turn.kind == "correction"
            run = not (correction and session.status == "finished")
            if run:
                deps = await db(
                    self._deps,
                    conn,
                    run_id=session.run_id,
                    plan_id=session.plan_id,
                    session_id=session.id,
                )
                async with open_workflow(self.dsn, deps, self.agent) as workflow:
                    # No interrupt means a node was interrupted mid-execution. Replaying
                    # None recovers its persisted resume value; it does not accept new text.
                    for _ in range(2):
                        command: Command[Any] | None = None
                        if receipt is not None and pending is not None:
                            if correction or pending["turn"] == receipt.turn.answers:
                                command = Command(resume={"turn": receipt.turn.ordinal})
                            else:
                                break  # already reached the following question
                        elif receipt is None and pending is not None:
                            break
                        if pending is None:
                            yield {"type": "recovered", "message": "Resuming saved interview work."}
                        async for mode, chunk in workflow.astream(
                            command, config, stream_mode=["custom"], durability="sync"
                        ):
                            if mode == "custom" and isinstance(chunk, dict):
                                yield chunk
                        snapshot = await workflow.aget_state(config)
                        pending = next(
                            (
                                w.value
                                for w in snapshot.interrupts
                                if isinstance(w.value, dict) and w.value.get("type") == "question"
                            ),
                            None,
                        )
                        if command is not None or pending is None or receipt is None:
                            break
            result = dict(pending) if run and pending is not None else {"type": "done"}
            if mid is not None:
                await db(
                    delivery.transition, conn, session.id, session.run_id, mid, "processed", result
                )
            complete = True
            yield result
        except (AuditError, ModelError) as error:
            yield {
                "type": "error",
                "code": "audit_unavailable"
                if isinstance(error, AuditError)
                else "model_unavailable",
                "message": "Processing paused. Resume the saved message.",
                "message_id": None if mid is None else str(mid),
            }
        finally:
            try:
                if not complete and mid is not None and not conn.closed:
                    await db(
                        delivery.transition, conn, session.id, session.run_id, mid, "needs_retry"
                    )
            except (AuditError, psycopg.Error):
                pass  # the durable processing receipt is recoverable on reconnect
            finally:
                await db(conn.close)


@dataclass
class MessageStream(AsyncIterator[Event]):
    """Own the lock even when a caller closes before starting iteration."""

    iterator: AsyncGenerator[Event]
    conn: psycopg.Connection
    message_id: UUID | None = None

    def __aiter__(self) -> "MessageStream":
        return self

    async def __anext__(self) -> Event:
        return await anext(self.iterator)

    async def aclose(self) -> None:
        try:
            await self.iterator.aclose()
        finally:
            await db(self.conn.close)


def build_service(settings: Settings) -> InterviewService:
    """The service as configured by the environment. Raises ``ConfigError`` without model names."""
    domains = work_domains(settings)

    def model(role: Literal["interviewer", "drafter"]) -> ChatModel:
        return LiteLLMModel(
            model_name(settings, role),
            timeout=settings.model_timeout_seconds,
            work_email_domains=domains,
        )

    return InterviewService(
        dsn=settings.database_url.get_secret_value(),
        drafter=model("drafter"),
        interviewer=model("interviewer"),
        embedder=HashingEmbedder(),
        work_email_domains=domains,
        tracer=build_tracer(settings),
        artifact_resolver=lambda conn, run_id: artifact_embedder_for_run(conn, run_id, settings),
        draft_configuration={"timeout_seconds": settings.model_timeout_seconds, "max_tokens": 4096},
    )
