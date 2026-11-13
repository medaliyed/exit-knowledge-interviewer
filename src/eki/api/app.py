"""The HTTP API: a liveness probe, plan review, and the interview chat.

There is no authentication in this phase: run it on localhost only (the default
of ``uvicorn``). Posting a message returns a Server-Sent Events stream of what
the interviewer did, ending with its next question, ``done`` or ``error``.
"""

import json
from collections.abc import AsyncIterator
from functools import lru_cache
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, ValidationError
from sse_starlette import EventSourceResponse, ServerSentEvent

from eki import __version__
from eki.config import ConfigError, get_settings
from eki.ingest.redaction import redact
from eki.interview.models import InterviewPlan
from eki.interview.plan import PlanError, PlanFrozenError, add_topic, remove_topic, reorder
from eki.interview.service import (
    ConflictError,
    InterviewService,
    NotFoundError,
    SessionView,
    build_service,
)
from eki.llm import ModelError

CONFIDENTIAL = "[confidential]"


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TopicToAdd(_Body):
    label: str
    note: str
    """What the interviewer should ask, in the expert's words."""
    position: int | None = None


class PlanEdit(_Body):
    """Applied in this order: remove, add, then order."""

    remove: list[str] = []
    add: list[TopicToAdd] = []
    order: list[str] | None = None


class Approval(_Body):
    approved_by: str


class ExpertMessage(_Body):
    action: Literal["answer", "skip", "correct"] = "answer"
    text: str = ""
    confidential: bool = False
    corrects: int | None = None
    """For ``correct``: the ordinal of the turn being corrected."""


def _plan(plan: InterviewPlan) -> dict[str, Any]:
    return plan.model_dump(mode="json")


def _session(view: SessionView) -> dict[str, Any]:
    waiting = view.waiting
    turns = [turn.model_dump(mode="json") for turn in view.turns]
    for turn in turns:
        if turn["confidential"]:
            turn["text"] = CONFIDENTIAL
    return {
        "id": str(view.session.id),
        "plan_id": str(view.session.plan_id),
        "run_id": str(view.session.run_id),
        "status": view.session.status,
        "current_topic_id": view.session.current_topic_id,
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


def create_app(service: InterviewService | None = None) -> FastAPI:
    """The application. Without ``service`` one is built from settings on first use."""
    app = FastAPI(title="Exit-Knowledge Interviewer", version=__version__)

    @lru_cache
    def configured() -> InterviewService:
        return build_service(get_settings())

    def get_service() -> InterviewService:
        return service if service is not None else configured()

    Service = Annotated[InterviewService, Depends(get_service)]

    def error(status: int) -> Any:
        async def handler(request: Request, exc: Exception) -> JSONResponse:
            return JSONResponse(status_code=status, content={"detail": str(exc)})

        return handler

    async def model_down(request: Request, exc: Exception) -> JSONResponse:
        detail = "The model could not be reached. Nothing was changed; try again."
        return JSONResponse(status_code=502, content={"detail": detail})

    async def not_configured(request: Request, exc: Exception) -> JSONResponse:
        detail = "The service is not configured: " + (
            str(exc) if isinstance(exc, ConfigError) else "check the EKI_ settings"
        )
        return JSONResponse(status_code=503, content={"detail": detail})

    app.add_exception_handler(NotFoundError, error(404))
    app.add_exception_handler(ConflictError, error(409))
    app.add_exception_handler(PlanFrozenError, error(409))
    app.add_exception_handler(PlanError, error(422))
    app.add_exception_handler(ModelError, model_down)
    app.add_exception_handler(ConfigError, not_configured)
    app.add_exception_handler(ValidationError, not_configured)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/runs/{run_id}/plan", status_code=201)
    async def draft_plan(run_id: UUID, service: Service) -> dict[str, Any]:
        """Draft an interview plan from the run's gap scores."""
        return _plan(await service.draft_plan(run_id))

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
            return plan if edit.order is None else reorder(plan, edit.order)

        return _plan(await service.edit_plan(plan_id, apply))

    @app.post("/plans/{plan_id}/approve")
    async def approve_plan(plan_id: UUID, approval: Approval, service: Service) -> dict[str, Any]:
        """Freeze the plan and open its session. Returns the session with the first question."""
        return _session(await service.approve(plan_id, approval.approved_by))

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
            )
        except ValueError as refused:
            raise HTTPException(status_code=422, detail=str(refused)) from refused

        async def events() -> AsyncIterator[ServerSentEvent]:
            async for event in stream:
                yield ServerSentEvent(data=json.dumps(event), event=str(event["type"]))

        return EventSourceResponse(events())

    return app


app = create_app()
