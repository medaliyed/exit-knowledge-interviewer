"""F21 endpoints. All mutations call the same transaction services as the CLI."""

from collections.abc import Callable
from datetime import date
from typing import Any
from uuid import UUID

import psycopg
from fastapi import FastAPI, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from eki import maintenance
from eki.api.security import identity
from eki.auth import Principal
from eki.graph.workflow import db
from eki.runbook.service import RunbookService


class Handover(BaseModel):
    model_config = ConfigDict(extra="forbid")
    owner_subject: str = Field(min_length=1, max_length=200)
    expected_owner_subject: str | None
    expected_run_revision: int = Field(ge=0)
    reason: str = Field(min_length=1, max_length=2000)


class Review(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)
    expected_maintenance_revision: int = Field(ge=0)
    review_by: date | None = None


class Withdrawal(Review):
    reason: str = Field(min_length=1, max_length=2000)


def install_maintenance(
    app: FastAPI, dsn: Callable[[], str], identities: Callable[[], tuple[Principal, ...]]
) -> None:
    async def operation(call: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        try:
            return await db(call)
        except ValueError as error:
            raise HTTPException(status_code=422, detail="Invalid maintenance change") from error

    @app.get("/runs/{run_id}/maintenance")
    async def run_state(run_id: UUID, request: Request) -> dict[str, Any]:
        def work() -> dict[str, Any]:
            with psycopg.connect(dsn(), autocommit=True) as conn:
                return maintenance.state(conn, run_id, identity(request))

        return await operation(work)

    @app.post("/runs/{run_id}/handover")
    async def handover(run_id: UUID, body: Handover, request: Request) -> dict[str, Any]:
        def work() -> dict[str, Any]:
            with psycopg.connect(dsn(), autocommit=True) as conn:
                return maintenance.transfer(
                    conn,
                    run_id,
                    body.owner_subject,
                    identity(request),
                    expected_owner=body.expected_owner_subject,
                    expected_revision=body.expected_run_revision,
                    reason=body.reason,
                    start=True,
                    identities=identities(),
                )

        return await operation(work)

    @app.post("/entries/{entry_id}/review")
    async def review_entry(entry_id: UUID, body: Review, request: Request) -> dict[str, Any]:
        def work() -> dict[str, Any]:
            books = RunbookService(dsn())
            books.review(entry_id, identity(request).name, **body.model_dump())
            return books.metadata(entry_id)

        return await operation(work)

    @app.post("/entries/{entry_id}/withdraw")
    async def withdraw_entry(entry_id: UUID, body: Withdrawal, request: Request) -> dict[str, Any]:
        def work() -> dict[str, Any]:
            books = RunbookService(dsn())
            values = body.model_dump(exclude={"review_by", "reason"})
            books.withdraw(entry_id, identity(request).name, body.reason, **values)
            return books.metadata(entry_id)

        return await operation(work)

    @app.get("/runs/{run_id}/maintenance/entries")
    async def entries(
        run_id: UUID,
        request: Request,
        filter: str = "all",
        limit: int = Query(50, ge=1, le=100),
        offset: int = Query(0, ge=0),
    ) -> dict[str, Any]:
        def work() -> dict[str, Any]:
            with psycopg.connect(dsn(), autocommit=True) as conn:
                return maintenance.queue(
                    conn, run_id, identity(request), filter=filter, limit=limit, offset=offset
                )

        return await operation(work)
