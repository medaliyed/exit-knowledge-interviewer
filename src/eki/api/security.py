"""Authenticate before resolving data dependencies and enforce cookie CSRF protection."""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import psycopg
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from eki import access, accounts, maintenance
from eki.audit import audit_context
from eki.auth import AuthenticationError, Authenticator, Principal, acting_as
from eki.config import ConfigError, Settings
from eki.errors import NotFoundError
from eki.graph.workflow import db

UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
PUBLIC_AUTH = frozenset({"/auth/login", "/auth/setup-password", "/auth/logout"})


def identity(request: Request) -> Principal:
    principal: Principal = request.state.principal
    return principal


def session_policy(settings: Settings) -> accounts.SessionPolicy:
    return accounts.SessionPolicy(
        idle=timedelta(hours=settings.session_idle_hours),
        max_age=timedelta(days=settings.session_max_days),
    )


def cookie_name(settings: Settings) -> str:
    # Browsers reject the __Host- prefix without Secure, even in development.
    return "__Host-eki_session" if settings.session_cookie_secure else "eki_session"


def install_security(
    app: FastAPI,
    authenticate: Callable[[], Authenticator],
    dsn: Callable[[], str],
    configuration: Callable[[], Settings],
) -> None:
    @app.middleware("http")
    async def secure(
        request: Request, following: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        parts = request.url.path.strip("/").split("/")
        is_auth = parts[0] == "auth"
        if parts[0] not in (
            "runs",
            "plans",
            "sessions",
            "entries",
            "runbook",
            "auth",
            "flags",
            "follow-up-requests",
            "follow-up-proposals",
        ):
            return await following(request)

        def private(response: Response) -> Response:
            response.headers["Cache-Control"] = "private, no-store"
            existing = response.headers.get("Vary", "")
            vary = [part.strip() for part in existing.split(",") if part.strip()]
            for name in ("Authorization", "Cookie"):
                if name.lower() not in {part.lower() for part in vary}:
                    vary.append(name)
            response.headers["Vary"] = ", ".join(vary)
            return response

        def rejected(status: int, detail: str, code: str) -> Response:
            return private(
                JSONResponse(status_code=status, content={"detail": detail, "code": code})
            )

        def csrf_valid() -> bool:
            return request.headers.get("origin") == configuration().public_origin

        header = request.headers.get("authorization")
        try:
            if request.url.path in PUBLIC_AUTH:
                if request.method in UNSAFE_METHODS and not csrf_valid():
                    return rejected(403, "Request origin is not allowed", "csrf_rejected")
                if request.method in UNSAFE_METHODS:
                    content_type = request.headers.get("content-type", "").partition(";")[0].strip()
                    if content_type != "application/json" and (
                        request.url.path != "/auth/logout" or await request.body()
                    ):
                        return rejected(
                            415, "Use an application/json request body", "json_required"
                        )
                return private(await following(request))

            settings = configuration()
            # An explicit header always wins, including an invalid bearer header. Without
            # one, a session cookie is tried; otherwise the bearer authenticator decides
            # (it fails closed, so a missing credential is still a 401).
            raw = None if header is not None else request.cookies.get(cookie_name(settings))
            bearer: Principal | None = None
            if not raw:
                bearer = authenticate().authenticate(header)
            request.state.authentication = "cookie" if raw else "bearer"

            def readable(conn: psycopg.Connection, principal: Principal, resource_id: UUID) -> bool:
                """Whether the request may proceed as a reader; raises NotFoundError if not."""
                run_id = access.resource_run(conn, parts[0], resource_id)
                if run_id is None:
                    raise NotFoundError("resource not found")
                try:
                    access.require_manager(conn, run_id, principal)
                except NotFoundError:
                    if (
                        parts[0] == "runs"
                        and request.method == "POST"
                        and parts[2:] == ["follow-up-requests"]
                    ):
                        access.visible_run(conn, resource_id, principal)
                        return True
                    if parts[0] == "entries" and (
                        (request.method == "GET" and (len(parts) == 2 or parts[2:] == ["markdown"]))
                        or (request.method == "POST" and parts[2:] == ["flags"])
                    ):
                        access.approved_entry(conn, resource_id, principal)
                        return True
                    raise
                return False

            def authorize() -> tuple[Principal, bool] | Response:
                """Session, account status and resource access, all on one connection."""
                with psycopg.connect(dsn(), autocommit=True) as conn:
                    if raw:
                        # A disabled account's session is refused here, so the cookie
                        # path needs no separate account-status check.
                        principal = accounts.authenticate_session(
                            conn, raw, now=datetime.now(UTC), policy=session_policy(settings)
                        )
                        if principal is None:
                            raise AuthenticationError("valid credentials required")
                        if request.method in UNSAFE_METHODS and not csrf_valid():
                            return rejected(403, "Request origin is not allowed", "csrf_rejected")
                    else:
                        assert bearer is not None  # noqa: S101 - set above when there is no cookie
                        principal = bearer
                        maintenance.active(conn, principal)
                    if (
                        is_auth
                        or principal.admin
                        or parts[0] in ("runbook", "flags", "follow-up-requests")
                        or len(parts) < 2
                    ):
                        return principal, False
                    try:
                        resource_id = UUID(parts[1])
                    except ValueError:
                        return rejected(422, "invalid identifier", "invalid_identifier")
                    return principal, readable(conn, principal, resource_id)

            if bearer is not None and is_auth and request.method == "GET":
                # Restoring a bearer identity needs no database at all.
                principal, reader = bearer, False
            else:
                outcome = await db(authorize)
                if isinstance(outcome, Response):
                    return outcome
                principal, reader = outcome
            request.state.principal = principal
            request.state.runbook_reader = reader
        except AuthenticationError:
            response = rejected(401, "valid credentials required", "session_expired")
            response.headers["WWW-Authenticate"] = "Bearer"
            return response
        except (ConfigError, ValidationError):
            return rejected(503, "authentication is not configured", "not_configured")
        except psycopg.Error:
            return rejected(503, "authentication is temporarily unavailable", "auth_unavailable")
        except NotFoundError:
            return rejected(404, "resource not found", "not_found")
        with acting_as(principal), audit_context(user_id=principal.subject, correlation_id=uuid4()):
            response = await following(request)
        return private(response)
