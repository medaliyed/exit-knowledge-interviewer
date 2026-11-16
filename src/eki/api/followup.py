"""F22 endpoints. Reader saves and receipts are separate from owner triage views."""

from collections.abc import Callable
from typing import Any, Literal
from uuid import UUID

import psycopg
from fastapi import FastAPI, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from eki.accounts import Throttled
from eki.api.security import identity
from eki.followup.gaps import GapService
from eki.followup.sessions import FollowUpService
from eki.graph.workflow import db
from eki.interview import store
from eki.interview.models import FollowUpLimits
from eki.runbook.models import SectionName


class FollowUpRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    parent_session_id: UUID
    gap_ids: list[UUID] = Field(min_length=1, max_length=50)
    limits: FollowUpLimits | None = None


class SaveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    question: str = Field(min_length=1, max_length=2000)
    entry_id: UUID | None = None
    section: SectionName | None = None


class Triage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["map", "new_topic", "defer", "dismiss", "reopen", "group"]
    expected_revision: int = Field(ge=0)
    target_entry_id: UUID | None = None
    target_section: SectionName | None = None
    seed_question: str | None = Field(default=None, max_length=2000)
    priority: int | None = Field(default=None, ge=0, le=10)
    reason: str | None = Field(default=None, max_length=2000)
    topic_label: str | None = Field(default=None, max_length=200)
    group_with: UUID | None = None


def install_followup(
    app: FastAPI, dsn: Callable[[], str], domains: Callable[[], frozenset[str]] = frozenset
) -> None:
    async def operation(call: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        try:
            return await db(call)
        except Throttled as error:
            raise HTTPException(
                status_code=429,
                detail="Follow-up request rate limit reached",
                headers={"Retry-After": str(error.retry_after)},
            ) from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail="Invalid follow-up change") from error

    def service() -> GapService:
        return GapService(dsn(), domains())

    @app.post("/runs/{run_id}/follow-up-requests")
    async def save_request(
        run_id: UUID, body: SaveRequest, request: Request, response: Response
    ) -> dict[str, Any]:
        def work() -> dict[str, Any]:
            saved, created = service().save_request(run_id, identity(request), **body.model_dump())
            response.status_code = 201 if created else 200
            return saved

        return await operation(work)

    @app.get("/follow-up-requests/{gap_id}")
    async def request_receipt(gap_id: UUID, request: Request) -> dict[str, Any]:
        return await operation(lambda: service().receipt(gap_id, identity(request)))

    @app.get("/runs/{run_id}/follow-up-gaps")
    async def gap_queue(
        run_id: UUID,
        request: Request,
        filter: str = "open",
        limit: int = Query(50, ge=1, le=100),
        offset: int = Query(0, ge=0),
    ) -> dict[str, Any]:
        return await operation(
            lambda: service().queue(
                run_id, identity(request), filter=filter, limit=limit, offset=offset
            )
        )

    @app.patch("/runs/{run_id}/follow-up-gaps/{gap_id}")
    async def triage_gap(
        run_id: UUID, gap_id: UUID, body: Triage, request: Request
    ) -> dict[str, Any]:
        return await operation(
            lambda: service().triage(run_id, gap_id, identity(request), **body.model_dump())
        )

    @app.post("/runs/{run_id}/follow-ups")
    async def prepare_follow_up(
        run_id: UUID, body: FollowUpRequest, request: Request, response: Response
    ) -> dict[str, Any]:
        def work() -> dict[str, Any]:
            with psycopg.connect(dsn(), autocommit=True) as conn:
                existed = store.load_plan(conn, body.request_id) is not None
            plan = FollowUpService(dsn(), domains()).prepare(
                run_id,
                identity(request),
                request_id=body.request_id,
                parent_session_id=body.parent_session_id,
                gap_ids=body.gap_ids,
                limits=body.limits,
            )
            response.status_code = 200 if existed else 201
            return plan.model_dump(mode="json")

        return await operation(work)

    @app.post("/sessions/{session_id}/stop")
    async def stop_session(session_id: UUID, request: Request) -> dict[str, Any]:
        return await operation(
            lambda: FollowUpService(dsn(), domains()).stop(session_id, identity(request))
        )
