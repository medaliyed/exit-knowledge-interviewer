"""Static browser shell and ACL-filtered navigation metadata."""

from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any
from uuid import UUID

import psycopg
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from eki import access
from eki.api.security import identity
from eki.graph.workflow import db

ASSETS = Path(__file__).parent / "static"


def install_browser(app: FastAPI, dsn: Callable[[], str]) -> None:
    app.mount("/assets", StaticFiles(directory=ASSETS), name="assets")

    @app.get("/", include_in_schema=False)
    @app.get("/workspace", include_in_schema=False)
    @app.get("/workspace/{path:path}", include_in_schema=False)
    def workspace(path: str = "") -> FileResponse:
        # Only browser paths use the SPA shell. Missing assets must remain 404s,
        # rather than returning HTML to a script/style request.
        if Path(path).suffix:
            raise HTTPException(status_code=404, detail="Not Found")
        return FileResponse(
            ASSETS / "workspace" / "index.html",
            headers={
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "default-src 'self'; script-src 'self'; "
                "style-src 'self'; connect-src 'self'; img-src 'self' data:; "
                "base-uri 'none'; frame-ancestors 'none'; "
                "form-action 'self'",
            },
        )

    @app.get("/runbook/identity")
    async def who(request: Request) -> dict[str, Any]:
        return asdict(identity(request))

    @app.get("/runs")
    async def runs(request: Request) -> list[dict[str, Any]]:
        principal = identity(request)

        def read() -> list[dict[str, Any]]:
            with psycopg.connect(dsn(), autocommit=True) as conn:
                rows = conn.execute(
                    "SELECT r.id, r.expert_name, r.since, r.until, r.status, r.owner_subject "
                    "FROM ingest_runs r WHERE "
                    + access.VISIBLE_RUN
                    + " ORDER BY r.started_at DESC, r.id LIMIT 200",
                    access.visible_params(principal),
                ).fetchall()
            return [
                {
                    "id": str(r[0]),
                    "expert_name": r[1],
                    "since": r[2].isoformat(),
                    "until": r[3].isoformat(),
                    "status": r[4],
                    "can_manage": principal.admin or r[5] == principal.subject,
                }
                for r in rows
            ]

        return await db(read)

    @app.get("/runs/{run_id}/plans")
    async def plans(run_id: UUID) -> list[dict[str, Any]]:
        def read() -> list[dict[str, Any]]:
            with psycopg.connect(dsn(), autocommit=True) as conn:
                rows = conn.execute(
                    "SELECT p.id, p.status, s.id FROM interview_plans p "
                    "LEFT JOIN sessions s ON s.plan_id=p.id WHERE p.run_id=%s "
                    "ORDER BY p.created_at DESC, p.id LIMIT 200",
                    (run_id,),
                ).fetchall()
            return [
                {"id": str(r[0]), "status": r[1], "session_id": None if r[2] is None else str(r[2])}
                for r in rows
            ]

        return await db(read)

    @app.get("/runbook/entries")
    async def entries(request: Request, run_id: UUID) -> list[dict[str, Any]]:
        principal = identity(request)

        def read() -> list[dict[str, Any]]:
            with psycopg.connect(dsn(), autocommit=True) as conn:
                rows = conn.execute(
                    "SELECT e.id, e.title, e.version FROM runbook_entries e "  # noqa: S608 - fixed ACL
                    "JOIN sessions s ON s.id=e.session_id JOIN interview_plans p ON p.id=s.plan_id "
                    "JOIN ingest_runs r ON r.id=p.run_id "
                    "WHERE r.id=%s AND e.status='approved' AND "
                    + access.VISIBLE_RUN
                    + " ORDER BY e.title, e.id LIMIT 200",
                    (run_id, *access.visible_params(principal)),
                ).fetchall()
            return [
                {"id": str(r[0]), "title": r[1], "version": r[2], "status": "approved"}
                for r in rows
            ]

        return await db(read)
