"""F21 endpoints. All mutations call the same transaction services as the CLI."""

from collections.abc import Callable
from typing import Any
from uuid import UUID

import psycopg
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from eki import maintenance
from eki.api.security import identity
from eki.auth import Principal
from eki.graph.workflow import db


class Handover(BaseModel):
    model_config = ConfigDict(extra="forbid")
    owner_subject: str = Field(min_length=1, max_length=200)
    expected_owner_subject: str | None
    expected_run_revision: int = Field(ge=0)
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
