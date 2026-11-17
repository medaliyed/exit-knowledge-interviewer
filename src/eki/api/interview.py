"""Plan drafting and review, and the interview chat with durable messages (F6-F8, F14, F18)."""

import json
from collections.abc import AsyncIterator
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sse_starlette import EventSourceResponse, ServerSentEvent

from eki import pool
from eki.api.context import ApiContext, Body, actor, operation
from eki.graph.workflow import db
from eki.ingest.redaction import redact
from eki.interview import generation
from eki.interview.models import FollowUpLimits, InterviewPlan
from eki.interview.plan import add_topic, edit_follow_up, remove_topic, reorder
from eki.interview.service import InterviewService, NotFoundError, SessionView
from eki.runbook.review import check_expert

CONFIDENTIAL = "[confidential]"


class TopicToAdd(Body):
    label: str
    note: str
    """What the interviewer should ask, in the expert's words."""
    position: int | None = None


class PlanEdit(BaseModel):
    """Applied in this order: remove, add, then order."""

    # Declared on BaseModel directly (not Body) so linters see the list defaults are
    # Pydantic fields, which are copied per instance.
    model_config = ConfigDict(extra="forbid")

    remove: list[str] = []
    add: list[TopicToAdd] = []
    order: list[str] | None = None
    seeds: dict[str, str] = {}
    remove_gaps: list[str] = []
    limits: FollowUpLimits | None = None
    expected_revision: int = Field(ge=1)


class PlanApproval(Body):
    approved_by: str
    expected_revision: int = Field(ge=1)
    """The plan revision the approver reviewed; a later edit makes approval conflict."""


class ExpertMessage(Body):
    message_id: UUID | None = None
    question_turn: int | None = None
    action: Literal["answer", "skip", "correct"] = "answer"
    text: str = ""
    confidential: bool = False
    corrects: int | None = None
    """For ``correct``: the ordinal of the turn being corrected."""


def _plan(plan: InterviewPlan) -> dict[str, Any]:
    return plan.model_dump(mode="json")


def _session(view: SessionView) -> dict[str, Any]:
    waiting = view.waiting
    turns = [
        turn.model_dump(
            mode="json",
            exclude={"author_subject", "author_name"} if turn.author_subject is None else set(),
        )
        for turn in view.turns
    ]
    for turn in turns:
        if turn.get("gap_id") is None:
            turn.pop("gap_id", None)
        if turn["confidential"]:
            turn["text"] = CONFIDENTIAL
    return {
        "id": str(view.session.id),
        "plan_id": str(view.session.plan_id),
        "run_id": str(view.session.run_id),
        "status": view.session.status,
        "current_topic_id": view.session.current_topic_id,
        "kind": view.session.kind,
        "completion_reason": view.session.completion_reason,
        "scope": list(view.scope),
        "usage": view.usage,
        "waiting": None
        if waiting is None
        else {
            "turn": waiting.ordinal,
            "question": waiting.text,
            "section": waiting.section,
            "topic_id": waiting.topic_id,
        },
        "checklist": view.checklist,
        "turns": turns,
        "notes": [note.model_dump(mode="json", exclude={"superseded"}) for note in view.notes],
        "open_items": [item.model_dump(mode="json") for item in view.open_items],
    }


def install_interview(app: FastAPI, ctx: ApiContext) -> None:
    Service = Annotated[InterviewService, Depends(ctx.service)]

    @app.post("/runs/{run_id}/plan", status_code=201)
    async def draft_plan(
        run_id: UUID, service: Service, generation_id: UUID | None = None
    ) -> dict[str, Any]:
        """Draft an interview plan from the run's gap scores."""
        return _plan(await service.draft_plan(run_id, generation_id=generation_id))

    @app.get("/runs/{run_id}/generations")
    async def generation_list(run_id: UUID) -> list[dict[str, Any]]:
        def read() -> list[dict[str, Any]]:
            with pool.connection(ctx.database_dsn()) as conn:
                return generation.listing(conn, run_id)

        return await db(read)

    @app.get("/runs/{run_id}/generations/{generation_id}")
    async def generation_status(run_id: UUID, generation_id: UUID) -> dict[str, Any]:
        def read() -> dict[str, Any]:
            with pool.connection(ctx.database_dsn()) as conn:
                return generation.read(conn, run_id, generation_id)

        return await db(read)

    @app.post("/runs/{run_id}/generations/{generation_id}/resume")
    async def resume_generation(
        run_id: UUID, generation_id: UUID, service: Service
    ) -> dict[str, Any]:
        await service.generation_status(run_id, generation_id)  # refuse unknown/cross-run IDs
        return _plan(await service.draft_plan(run_id, generation_id=generation_id))

    @app.get("/plans/{plan_id}")
    async def read_plan(plan_id: UUID, service: Service) -> dict[str, Any]:
        return _plan(await service.get_plan(plan_id))

    @app.patch("/plans/{plan_id}")
    async def edit_plan(plan_id: UUID, edit: PlanEdit, service: Service) -> dict[str, Any]:
        """Remove, add or reorder topics. Refused once the plan is approved."""
        domains = service.work_email_domains

        def apply(plan: InterviewPlan) -> InterviewPlan:
            for topic_id in edit.remove:
                plan = remove_topic(plan, topic_id)
            for item in edit.add:
                plan = add_topic(
                    plan,
                    redact(item.label, work_email_domains=domains).text,
                    redact(item.note, work_email_domains=domains).text,
                    position=item.position,
                )
            if edit.seeds or edit.remove_gaps or edit.limits is not None:
                plan = edit_follow_up(
                    plan,
                    seeds={
                        g: redact(text, work_email_domains=domains).text
                        for g, text in edit.seeds.items()
                    },
                    remove_gaps=edit.remove_gaps,
                    limits=edit.limits,
                )
            return plan if edit.order is None else reorder(plan, edit.order)

        return _plan(
            await service.edit_plan(
                plan_id,
                apply,
                expected_revision=edit.expected_revision,
            )
        )

    @app.post("/plans/{plan_id}/approve")
    async def approve_plan(
        plan_id: UUID, approval: PlanApproval, service: Service, request: Request
    ) -> dict[str, Any]:
        """Freeze the plan and open its session. Returns the session with the first question."""
        actor(request, approval.approved_by)

        def expert() -> None:
            with pool.connection(service.dsn) as conn:
                row = conn.execute(
                    "SELECT r.expert_name FROM ingest_runs r "
                    "JOIN interview_plans p ON p.run_id=r.id WHERE p.id=%s",
                    (plan_id,),
                ).fetchone()
            if row is None:
                raise NotFoundError(f"no plan {plan_id}")
            check_expert(approval.approved_by, row[0])

        await operation(expert)
        return _session(
            await service.approve(
                plan_id,
                approval.approved_by,
                expected_revision=approval.expected_revision,
            )
        )

    @app.get("/sessions/{session_id}")
    async def read_session(session_id: UUID, service: Service) -> dict[str, Any]:
        return _session(await service.view(session_id))

    @app.post("/sessions/{session_id}/messages", response_class=EventSourceResponse)
    async def post_message(
        session_id: UUID, message: ExpertMessage, service: Service
    ) -> EventSourceResponse:
        """Send an answer, a skip or a correction. The reply is an event stream."""
        try:
            stream = await service.post(
                session_id,
                message.action,
                message.text,
                confidential=message.confidential,
                corrects=message.corrects,
                message_id=message.message_id,
                question_turn=message.question_turn,
            )
        except ValueError as refused:
            raise HTTPException(status_code=422, detail=str(refused)) from refused

        return message_response(stream)

    def message_response(stream: Any) -> EventSourceResponse:
        async def events() -> AsyncIterator[ServerSentEvent]:
            try:
                async for event in stream:
                    yield ServerSentEvent(data=json.dumps(event), event=str(event["type"]))
            finally:
                await stream.aclose()

        headers = {} if stream.message_id is None else {"X-EKI-Message-ID": str(stream.message_id)}
        return EventSourceResponse(events(), headers=headers)

    @app.get("/sessions/{session_id}/messages")
    async def pending_messages(session_id: UUID, service: Service) -> list[dict[str, Any]]:
        return await service.receipts(session_id)

    @app.get("/sessions/{session_id}/messages/{message_id}")
    async def message_status(
        session_id: UUID, message_id: UUID, service: Service
    ) -> dict[str, Any]:
        return await service.receipt(session_id, message_id)

    @app.post("/sessions/{session_id}/messages/{message_id}/resume")
    async def resume_message(
        session_id: UUID, message_id: UUID, service: Service
    ) -> EventSourceResponse:
        return message_response(await service.resume(session_id, message_id))

    @app.post("/sessions/{session_id}/resume")
    async def resume_session(session_id: UUID, service: Service) -> EventSourceResponse:
        return message_response(await service.resume(session_id))
