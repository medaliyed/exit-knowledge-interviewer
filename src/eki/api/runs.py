"""Run access lists, ownership and audit history (F11, F13, F21)."""

from typing import Annotated
from uuid import UUID

from fastapi import Depends, FastAPI, Request

from eki import access, pool
from eki.api.context import ApiContext, Body, operation
from eki.api.security import identity
from eki.audit import list_events
from eki.graph.workflow import db
from eki.search.service import SearchService


class Readers(Body):
    readers: list[str]


class Owner(Body):
    subject: str
    expected_owner_subject: str | None = None
    expected_run_revision: int | None = None
    reason: str = "Owner assignment"


def install_runs(app: FastAPI, ctx: ApiContext) -> None:
    Search = Annotated[SearchService, Depends(ctx.search)]

    @app.get("/runs/{run_id}/access")
    async def read_access(run_id: UUID, request: Request) -> dict[str, object]:
        def work() -> dict[str, object]:
            with pool.connection(ctx.database_dsn()) as conn:
                return access.readers(conn, run_id, identity(request))

        return await db(work)

    @app.put("/runs/{run_id}/access")
    async def update_access(run_id: UUID, body: Readers, request: Request) -> dict[str, object]:
        def work() -> dict[str, object]:
            with pool.connection(ctx.database_dsn()) as conn:
                access.set_readers(conn, run_id, body.readers, identity(request))
                return access.readers(conn, run_id, identity(request))

        return await operation(work)

    @app.put("/runs/{run_id}/owner")
    async def update_owner(run_id: UUID, body: Owner, request: Request) -> dict[str, object]:
        def work() -> dict[str, object]:
            with pool.connection(ctx.database_dsn()) as conn:
                access.set_owner(
                    conn,
                    run_id,
                    body.subject,
                    identity(request),
                    expected_owner=body.expected_owner_subject,
                    expected_revision=body.expected_run_revision,
                    reason=body.reason,
                    identities=tuple(getattr(ctx.authenticator(), "identities", ())),
                )
                return access.readers(conn, run_id, identity(request))

        return await operation(work)

    @app.get("/runs/{run_id}/audit")
    async def read_audit(
        run_id: UUID, request: Request, search: Search, after: int = 0, limit: int = 100
    ) -> dict[str, object]:
        def read() -> list[dict[str, object]]:
            with pool.connection(search.dsn) as conn:
                if not identity(request).admin:
                    access.require_manager(conn, run_id, identity(request))
                return list_events(conn, run_id, after=after, limit=limit)

        rows = await operation(read)
        return {"events": rows, "next_after": rows[-1]["id"] if rows else after}
