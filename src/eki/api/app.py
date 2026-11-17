"""The HTTP API: app wiring, error mapping and liveness; routes live in their area modules.

All data routes require a bearer or session identity; run ownership and reader
ACLs determine which data is visible. Posting a message returns a Server-Sent Events stream of what
the interviewer did, ending with its next question, ``done`` or ``error``.
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import lru_cache
from typing import Any

import psycopg
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from eki import __version__, accounts
from eki.api import access_log
from eki.api.accounts import install_accounts
from eki.api.context import ApiContext
from eki.api.entries import install_entries
from eki.api.followup import install_followup
from eki.api.interview import install_interview
from eki.api.knowledge import install_knowledge
from eki.api.maintenance import install_maintenance
from eki.api.runs import install_runs
from eki.api.security import install_security
from eki.audit import AuditError
from eki.auth import AuthenticationError, Authenticator, ForbiddenError, TokenAuthenticator
from eki.config import ConfigError, Settings, get_settings, work_domains
from eki.db import pending_warning
from eki.graph.workflow import db
from eki.interview.plan import (
    PlanError,
    PlanFrozenError,
)
from eki.interview.service import (
    ConflictError,
    GenerationFailed,
    InterviewService,
    NotFoundError,
    build_service,
)
from eki.llm import ModelError
from eki.runbook.service import RunbookService
from eki.search.answers import AnswerService, build_answers
from eki.search.jobs import IndexWorker
from eki.search.service import SearchService, build_search
from eki.web.routes import install_browser


class AuthenticatedAPI(FastAPI):
    """Document both forms of authentication enforced by the middleware."""

    def openapi(self) -> dict[str, Any]:
        if self.openapi_schema is None:
            schema = get_openapi(title=self.title, version=self.version, routes=self.routes)
            schema.setdefault("components", {}).setdefault("securitySchemes", {})["BearerAuth"] = {
                "type": "http",
                "scheme": "bearer",
            }
            schema["components"]["securitySchemes"]["SessionCookie"] = {
                "type": "apiKey",
                "in": "cookie",
                "name": "__Host-eki_session",
            }
            for path, operations in schema["paths"].items():
                if path not in ("/healthz", "/auth/login", "/auth/setup-password", "/auth/logout"):
                    for operation in operations.values():
                        operation["security"] = [{"BearerAuth": []}, {"SessionCookie": []}]
            self.openapi_schema = schema
        return self.openapi_schema


def create_app(
    service: InterviewService | None = None,
    *,
    runbook_service: RunbookService | None = None,
    authenticator: Authenticator | None = None,
    search_service: SearchService | None = None,
    answer_service: AnswerService | None = None,
    settings: Settings | None = None,
) -> FastAPI:
    """The application. Without ``service`` one is built from settings on first use."""

    def configuration() -> Settings:
        return settings if settings is not None else get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configuration()  # Reject insecure production settings before accepting requests.

        def check_migrations() -> None:
            try:
                with psycopg.connect(database_dsn(), autocommit=True) as conn:
                    warning = pending_warning(conn)
            except psycopg.Error:
                return  # an unreachable database is reported by the first request
            if warning is not None:
                logging.getLogger(__name__).warning(warning)

        await db(check_migrations)
        bearer = get_authenticator()
        if isinstance(bearer, TokenAuthenticator):

            def check_subjects() -> None:
                try:
                    with psycopg.connect(database_dsn(), autocommit=True) as conn:
                        accounts.validate_bearer_identities(conn, bearer.identities)
                except psycopg.Error:
                    # No database yet (or migrations pending): the same check runs again
                    # on every sign-in, so starting is safe. A real conflict still refuses.
                    logging.getLogger(__name__).warning(
                        "bearer/account identity check skipped: database unavailable"
                    )

            await db(check_subjects)
        search = get_search()
        worker = IndexWorker(search.dsn, search.embedder, search.work_email_domains)
        stop = asyncio.Event()
        task = asyncio.create_task(worker.serve(stop))
        try:
            yield
        finally:
            stop.set()
            await task

    app = AuthenticatedAPI(
        title="Exit-Knowledge Interviewer", version=__version__, lifespan=lifespan
    )
    access_log.install()

    def database_dsn() -> str:
        if answer_service is not None:
            return answer_service.search.dsn
        if search_service is not None:
            return search_service.dsn
        if runbook_service is not None:
            return runbook_service.dsn
        if service is not None:
            return service.dsn
        return configuration().database_url.get_secret_value()

    def get_authenticator() -> Authenticator:
        return authenticator or TokenAuthenticator(configuration().api_tokens.get_secret_value())

    install_security(app, get_authenticator, database_dsn, configuration)
    install_accounts(app, database_dsn, configuration)
    install_browser(app, database_dsn)
    install_maintenance(
        app,
        database_dsn,
        lambda: tuple(getattr(get_authenticator(), "identities", ())),
        lambda: work_domains(configuration()),
    )
    install_followup(app, database_dsn, lambda: work_domains(configuration()))

    @lru_cache
    def configured() -> InterviewService:
        return build_service(configuration())

    def get_service() -> InterviewService:
        return service if service is not None else configured()

    def get_runbooks() -> RunbookService:
        if runbook_service is not None:
            return runbook_service
        if service is not None:
            return RunbookService(service.dsn, service.work_email_domains)
        settings = configuration()
        return RunbookService(settings.database_url.get_secret_value(), work_domains(settings))

    def get_search() -> SearchService:
        if answer_service is not None:
            return answer_service.search
        if search_service is not None:
            return search_service
        if service is not None:
            return SearchService(service.dsn, service.work_email_domains)
        if runbook_service is not None:
            return SearchService(runbook_service.dsn, runbook_service.work_email_domains)
        return build_search(configuration())

    def get_answers() -> AnswerService:
        if answer_service is not None:
            return answer_service
        if search_service is not None or service is not None or runbook_service is not None:
            return AnswerService(get_search())
        return build_answers(configuration(), get_search())

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Pydantic normally echoes rejected input, which may contain an unredacted secret.
        return JSONResponse(status_code=422, content={"detail": "Invalid request value"})

    def error(status: int) -> Any:
        async def handler(request: Request, exc: Exception) -> JSONResponse:
            return JSONResponse(status_code=status, content={"detail": str(exc)})

        return handler

    async def model_down(request: Request, exc: Exception) -> JSONResponse:
        detail = "The model could not be reached. Try again when the provider is available."
        body: dict[str, Any] = {"detail": detail}
        if isinstance(exc, GenerationFailed):
            body.update(
                detail="Model unavailable. Drafting paused; resume the saved generation.",
                generation_id=str(exc.generation_id),
            )
        return JSONResponse(status_code=502, content=body)

    async def not_configured(request: Request, exc: Exception) -> JSONResponse:
        detail = "The service is not configured: " + (
            str(exc) if isinstance(exc, ConfigError) else "check the EKI_ settings"
        )
        return JSONResponse(status_code=503, content={"detail": detail})

    app.add_exception_handler(AuditError, error(503))
    app.add_exception_handler(ForbiddenError, error(403))
    app.add_exception_handler(AuthenticationError, error(401))
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

    context = ApiContext(
        database_dsn=database_dsn,
        authenticator=get_authenticator,
        service=get_service,
        runbooks=get_runbooks,
        search=get_search,
        answers=get_answers,
    )
    install_entries(app, context)
    install_interview(app, context)
    install_runs(app, context)
    install_knowledge(app, context)

    return app


app = create_app()
