"""Cookie-session endpoints. Passwords stay secret-typed until the account boundary."""

from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

import psycopg
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from eki import accounts
from eki.api.security import cookie_name, identity, session_policy
from eki.config import Settings
from eki.graph.workflow import db

INVALID_LINK = "This link is no longer valid. Ask the person who invited you for a new one."


class Login(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    email: str = Field(max_length=254)
    password: SecretStr = Field(max_length=1024)


class SetupPassword(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    token: SecretStr = Field(max_length=256)
    new_password: SecretStr = Field(max_length=1024)


class ChangePassword(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    current_password: SecretStr = Field(max_length=1024)
    new_password: SecretStr = Field(max_length=1024)


def auth_error(status: int, detail: str, code: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"detail": detail, "code": code})


def install_accounts(
    app: FastAPI, dsn: Callable[[], str], configuration: Callable[[], Settings]
) -> None:
    def connection() -> psycopg.Connection:
        return psycopg.connect(dsn(), autocommit=True)

    def public_identity(conn: psycopg.Connection, request: Request) -> dict[str, Any]:
        principal = identity(request)
        email = None
        if request.state.authentication == "cookie":
            row = conn.execute(
                "SELECT email FROM accounts WHERE subject = %s", (principal.subject,)
            ).fetchone()
            email = row[0] if row else None
        return {**asdict(principal), "email": email}

    def signed_response(conn: psycopg.Connection, signed: accounts.Signed) -> JSONResponse:
        row = conn.execute(
            "SELECT email FROM accounts WHERE subject = %s", (signed.principal.subject,)
        ).fetchone()
        response = JSONResponse({**asdict(signed.principal), "email": row[0] if row else None})
        settings = configuration()
        response.set_cookie(
            cookie_name(settings),
            signed.session_id,
            max_age=int(session_policy(settings).max_age.total_seconds()),
            secure=settings.session_cookie_secure,
            httponly=True,
            samesite="strict",
            path="/",
        )
        return response

    @app.exception_handler(accounts.Throttled)
    async def throttled(request: Request, exc: accounts.Throttled) -> JSONResponse:
        response = auth_error(429, "Too many attempts. Try again later.", "throttled")
        response.headers["Retry-After"] = str(exc.retry_after)
        return response

    @app.exception_handler(accounts.PasswordPolicyError)
    async def weak_password(request: Request, exc: accounts.PasswordPolicyError) -> JSONResponse:
        messages = {
            "too_short": "Use at least 12 characters.",
            "too_long": "Use no more than 128 characters.",
            "common": "Choose a password that is not common or breached.",
            "contains_identity": "Your password must not contain your email address or name.",
            "unchanged": "Choose a password different from your current password.",
            "not_printable": "Use printable characters and spaces.",
        }
        return auth_error(
            400, messages.get(exc.code, "Choose a stronger password."), "weak_password"
        )

    @app.post("/auth/login")
    async def login(body: Login, request: Request) -> Response:
        def work() -> Response:
            with connection() as conn:
                try:
                    signed = accounts.sign_in(
                        conn,
                        body.email,
                        body.password,
                        now=datetime.now(UTC),
                        client=request.client.host if request.client else "unknown",
                        policy=session_policy(configuration()),
                        user_agent=request.headers.get("user-agent", ""),
                    )
                except accounts.SignInRefused:
                    return auth_error(401, "Email or password is incorrect", "invalid_credentials")
                return signed_response(conn, signed)

        return await db(work)

    @app.post("/auth/setup-password")
    async def setup_password(body: SetupPassword, request: Request) -> Response:
        def work() -> Response:
            with connection() as conn:
                try:
                    signed = accounts.redeem_link(
                        conn,
                        body.token.get_secret_value(),
                        body.new_password,
                        now=datetime.now(UTC),
                        policy=session_policy(configuration()),
                        client=request.client.host if request.client else "unknown",
                        user_agent=request.headers.get("user-agent", ""),
                    )
                except accounts.LinkError:
                    return auth_error(400, INVALID_LINK, "invalid_link")
                return signed_response(conn, signed)

        return await db(work)

    @app.get("/auth/session")
    async def session(request: Request) -> dict[str, Any]:
        # Bearer-only callers do not need an account store.
        if request.state.authentication == "bearer":
            return {**asdict(identity(request)), "email": None}

        def work() -> dict[str, Any]:
            with connection() as conn:
                return public_identity(conn, request)

        return await db(work)

    @app.post("/auth/logout", status_code=204)
    async def logout(request: Request) -> Response:
        settings = configuration()
        raw = request.cookies.get(cookie_name(settings))
        if raw:

            def work() -> None:
                with connection() as conn:
                    accounts.sign_out(conn, raw)

            await db(work)
        response = Response(status_code=204)
        response.delete_cookie(
            cookie_name(settings),
            path="/",
            secure=settings.session_cookie_secure,
            httponly=True,
            samesite="strict",
        )
        return response

    @app.post("/auth/password", status_code=204)
    async def password(body: ChangePassword, request: Request) -> Response:
        if request.state.authentication != "cookie":
            return auth_error(401, "Sign in with your email and password.", "session_required")

        def work() -> Response:
            with connection() as conn:
                try:
                    accounts.change_password(
                        conn,
                        request.cookies[cookie_name(configuration())],
                        body.current_password,
                        body.new_password,
                        now=datetime.now(UTC),
                        policy=session_policy(configuration()),
                    )
                except accounts.SignInRefused:
                    return auth_error(
                        400, "Current password is incorrect.", "current_password_incorrect"
                    )
            return Response(status_code=204)

        return await db(work)
