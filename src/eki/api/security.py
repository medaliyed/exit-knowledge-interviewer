"""Authenticate before resolving data dependencies; protect legacy routes as well."""

from collections.abc import Awaitable, Callable
from uuid import UUID

import psycopg
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from eki import access
from eki.auth import AuthenticationError, Authenticator, Principal
from eki.config import ConfigError
from eki.errors import NotFoundError
from eki.graph.workflow import db


def identity(request: Request) -> Principal:
    principal: Principal = request.state.principal
    return principal


def install_security(
    app: FastAPI, authenticate: Callable[[], Authenticator], dsn: Callable[[], str]
) -> None:
    @app.middleware("http")
    async def secure(
        request: Request, following: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        parts = request.url.path.strip("/").split("/")
        if parts[0] not in ("runs", "plans", "sessions", "entries", "runbook"):
            return await following(request)
        header = request.headers.get("authorization")
        try:
            principal = authenticate().authenticate(header)
            request.state.principal = principal
            request.state.runbook_reader = False
            if not principal.admin and parts[0] != "runbook" and len(parts) > 1:
                try:
                    resource_id = UUID(parts[1])
                except ValueError:
                    return JSONResponse(status_code=422, content={"detail": "invalid identifier"})

                def check() -> bool:
                    with psycopg.connect(dsn(), autocommit=True) as conn:
                        run_id = access.resource_run(conn, parts[0], resource_id)
                        if run_id is None:
                            raise NotFoundError("resource not found")
                        try:
                            access.require_manager(conn, run_id, principal)
                        except NotFoundError:
                            if (
                                parts[0] == "entries"
                                and request.method == "GET"
                                and (len(parts) == 2 or parts[2:] == ["markdown"])
                            ):
                                access.approved_entry(conn, resource_id, principal)
                                return True
                            raise
                        return False

                request.state.runbook_reader = await db(check)
        except AuthenticationError:
            return JSONResponse(
                status_code=401,
                content={"detail": "valid bearer credentials required"},
                headers={"WWW-Authenticate": "Bearer"},
            )
        except (ConfigError, ValidationError):
            return JSONResponse(
                status_code=503, content={"detail": "authentication is not configured"}
            )
        except NotFoundError:
            return JSONResponse(status_code=404, content={"detail": "resource not found"})
        return await following(request)
