"""The HTTP API: liveness, plan review, interview chat and private runbook review.

All data routes require a configured bearer identity; run ownership and reader
ACLs determine which data is visible. Posting a message returns a Server-Sent Events stream of what
the interviewer did, ending with its next question, ``done`` or ``error``.
"""

import json
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import asdict
from functools import lru_cache
from typing import Annotated, Any, Literal
from uuid import UUID

import psycopg
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, ValidationError
from sse_starlette import EventSourceResponse, ServerSentEvent

from eki import __version__, access
from eki.api import access_log
from eki.api.security import identity, install_security
from eki.audit import AuditError, list_events
from eki.auth import Authenticator, ForbiddenError, TokenAuthenticator
from eki.config import ConfigError, get_settings, work_domains
from eki.graph.workflow import db
from eki.ingest.redaction import redact
from eki.interview import generation
from eki.interview.models import InterviewPlan
from eki.interview.plan import PlanError, PlanFrozenError, add_topic, remove_topic, reorder
from eki.interview.service import (
    ConflictError,
    GenerationFailed,
    InterviewService,
    NotFoundError,
    SessionView,
    build_service,
)
from eki.llm import ModelError
from eki.runbook.export import ExportService
from eki.runbook.faithfulness import Unsupported
from eki.runbook.models import SECTION_NAMES, SectionName
from eki.runbook.review import SectionEdit, check_expert
from eki.runbook.service import RunbookService
from eki.runbook.store import StoredEntry
from eki.search.answers import AnswerService, build_answers
from eki.search.service import SearchService, build_search
from eki.web.routes import install_browser

CONFIDENTIAL = "[confidential]"


class AuthenticatedAPI(FastAPI):
    """Document the bearer authentication enforced by the data-route middleware."""

    def openapi(self) -> dict[str, Any]:
        if self.openapi_schema is None:
            schema = get_openapi(title=self.title, version=self.version, routes=self.routes)
            schema.setdefault("components", {}).setdefault("securitySchemes", {})["BearerAuth"] = {
                "type": "http",
                "scheme": "bearer",
            }
            for path, operations in schema["paths"].items():
                if path != "/healthz":
                    for operation in operations.values():
                        operation["security"] = [{"BearerAuth": []}]
            self.openapi_schema = schema
        return self.openapi_schema


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
    message_id: UUID | None = None
    question_turn: int | None = None
    action: Literal["answer", "skip", "correct"] = "answer"
    text: str = ""
    confidential: bool = False
    corrects: int | None = None
    """For ``correct``: the ordinal of the turn being corrected."""


class RunbookQuestion(_Body):
    question: str


class RunbookSearch(_Body):
    query: str
    limit: int = 5


class Readers(_Body):
    readers: list[str]


class Owner(_Body):
    subject: str


class EntryEdit(_Body):
    edited_by: str
    sections: dict[SectionName, SectionEdit]


class Rejection(_Body):
    rejected_by: str
    reason: str


def _entry_summary(stored: StoredEntry, unsupported: Sequence[Unsupported]) -> dict[str, Any]:
    entry = stored.entry
    return {
        "id": entry.id,
        "topic_id": entry.topic_id,
        "title": entry.title,
        "status": entry.status.value,
        "version": stored.version,
        "edited": stored.edited,
        "unsupported_count": len(unsupported),
        "claim_count": sum(len(entry.section(n).claims) for n in SECTION_NAMES),
        "open_question_count": sum(
            entry.section(n).open_question is not None for n in SECTION_NAMES
        ),
    }


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


def create_app(
    service: InterviewService | None = None,
    *,
    runbook_service: RunbookService | None = None,
    authenticator: Authenticator | None = None,
    search_service: SearchService | None = None,
    answer_service: AnswerService | None = None,
) -> FastAPI:
    """The application. Without ``service`` one is built from settings on first use."""
    app = AuthenticatedAPI(title="Exit-Knowledge Interviewer", version=__version__)
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
        return get_settings().database_url.get_secret_value()

    def get_authenticator() -> Authenticator:
        return authenticator or TokenAuthenticator(get_settings().api_tokens.get_secret_value())

    install_security(app, get_authenticator, database_dsn)
    install_browser(app, database_dsn)

    def actor(request: Request, name: str) -> None:
        if name.strip() != identity(request).name:
            raise HTTPException(
                status_code=422, detail="reviewer must match the authenticated identity"
            )

    @lru_cache
    def configured() -> InterviewService:
        return build_service(get_settings())

    def get_service() -> InterviewService:
        return service if service is not None else configured()

    Service = Annotated[InterviewService, Depends(get_service)]

    def get_runbooks() -> RunbookService:
        if runbook_service is not None:
            return runbook_service
        if service is not None:
            return RunbookService(service.dsn, service.work_email_domains)
        settings = get_settings()
        return RunbookService(settings.database_url.get_secret_value(), work_domains(settings))

    Runbooks = Annotated[RunbookService, Depends(get_runbooks)]

    def get_search() -> SearchService:
        if answer_service is not None:
            return answer_service.search
        if search_service is not None:
            return search_service
        if service is not None:
            return SearchService(service.dsn, service.work_email_domains)
        if runbook_service is not None:
            return SearchService(runbook_service.dsn, runbook_service.work_email_domains)
        return build_search(get_settings())

    Search = Annotated[SearchService, Depends(get_search)]

    def get_answers() -> AnswerService:
        if answer_service is not None:
            return answer_service
        if search_service is not None or service is not None or runbook_service is not None:
            return AnswerService(get_search())
        return build_answers(get_settings(), get_search())

    Answers = Annotated[AnswerService, Depends(get_answers)]

    async def operation[T](call: Callable[[], T]) -> T:
        try:
            return await db(call)
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail="invalid runbook change") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    def summaries(books: RunbookService, session_id: UUID) -> list[dict[str, Any]]:
        unsupported = books.unsupported(session_id)
        return [_entry_summary(e, unsupported.get(e.entry.id, ())) for e in books.list(session_id)]

    async def detail(books: RunbookService, entry_id: UUID) -> dict[str, Any]:
        def work() -> dict[str, Any]:
            stored = books.get(entry_id)
            unsupported = books.unsupported(stored.session_id).get(stored.entry.id, ())
            return {
                **stored.entry.model_dump(mode="json"),
                "session_id": str(stored.session_id),
                "version": stored.version,
                "rejection_reason": stored.rejection_reason,
                # Claims that say more than the answer they cite; see eki.runbook.faithfulness.
                "unsupported": [
                    {"section": u.section, "claim_index": u.index, "terms": list(u.terms)}
                    for u in unsupported
                ],
                "decisions": [asdict(d) for d in books.history(entry_id)],
            }

        return await db(work)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Pydantic normally echoes rejected input, which may contain an unredacted secret.
        return JSONResponse(status_code=422, content={"detail": "Invalid request value"})

    @app.post("/sessions/{session_id}/synthesise")
    async def synthesise_entries(
        session_id: UUID, books: Runbooks, overwrite_edits: bool = False
    ) -> list[dict[str, Any]]:
        """Rebuild drafts from the notes. Hand-edited drafts are kept unless told otherwise."""
        await operation(lambda: books.synthesise(session_id, overwrite_edits=overwrite_edits))
        return await db(summaries, books, session_id)

    @app.get("/sessions/{session_id}/entries")
    async def list_entries(session_id: UUID, books: Runbooks) -> list[dict[str, Any]]:
        return await db(summaries, books, session_id)

    @app.get("/entries/{entry_id}")
    async def read_entry(entry_id: UUID, books: Runbooks, request: Request) -> dict[str, Any]:
        if request.state.runbook_reader:

            def read() -> dict[str, Any]:
                with books._connect() as conn:
                    stored = access.approved_entry(conn, entry_id, identity(request))
                return {**stored.entry.model_dump(mode="json"), "version": stored.version}

            return await db(read)
        return await detail(books, entry_id)

    @app.patch("/entries/{entry_id}")
    async def edit_entry(
        entry_id: UUID, edit: EntryEdit, books: Runbooks, request: Request
    ) -> dict[str, Any]:
        actor(request, edit.edited_by)
        await operation(lambda: books.edit(entry_id, edit.edited_by, edit.sections))
        return await detail(books, entry_id)

    @app.post("/entries/{entry_id}/approve")
    async def approve_entry(
        entry_id: UUID, approval: Approval, books: Runbooks, request: Request
    ) -> dict[str, Any]:
        actor(request, approval.approved_by)
        await operation(lambda: books.approve(entry_id, approval.approved_by))
        return await detail(books, entry_id)

    @app.post("/entries/{entry_id}/reject")
    async def reject_entry(
        entry_id: UUID, rejection: Rejection, books: Runbooks, request: Request
    ) -> dict[str, Any]:
        actor(request, rejection.rejected_by)
        await operation(lambda: books.reject(entry_id, rejection.rejected_by, rejection.reason))
        return await detail(books, entry_id)

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
    async def draft_plan(
        run_id: UUID, service: Service, generation_id: UUID | None = None
    ) -> dict[str, Any]:
        """Draft an interview plan from the run's gap scores."""
        return _plan(await service.draft_plan(run_id, generation_id=generation_id))

    @app.get("/runs/{run_id}/generations")
    async def generation_list(run_id: UUID, books: Runbooks) -> list[dict[str, Any]]:
        def read() -> list[dict[str, Any]]:
            with books._connect() as conn:
                return generation.listing(conn, run_id)

        return await db(read)

    @app.get("/runs/{run_id}/generations/{generation_id}")
    async def generation_status(
        run_id: UUID, generation_id: UUID, books: Runbooks
    ) -> dict[str, Any]:
        def read() -> dict[str, Any]:
            with books._connect() as conn:
                return generation.read(conn, run_id, generation_id)

        return await db(read)

    @app.post("/runs/{run_id}/generations/{generation_id}/resume")
    async def resume_generation(
        run_id: UUID, generation_id: UUID, service: Service
    ) -> dict[str, Any]:
        await service.generation_status(run_id, generation_id)  # refuse unknown/cross-run IDs
        return _plan(await service.draft_plan(run_id, generation_id=generation_id))

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
    async def approve_plan(
        plan_id: UUID, approval: Approval, service: Service, request: Request
    ) -> dict[str, Any]:
        """Freeze the plan and open its session. Returns the session with the first question."""
        actor(request, approval.approved_by)

        def expert() -> None:
            with psycopg.connect(service.dsn, autocommit=True) as conn:
                row = conn.execute(
                    "SELECT r.expert_name FROM ingest_runs r "
                    "JOIN interview_plans p ON p.run_id=r.id WHERE p.id=%s",
                    (plan_id,),
                ).fetchone()
            if row is None:
                raise NotFoundError(f"no plan {plan_id}")
            check_expert(approval.approved_by, row[0])

        await operation(expert)
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
                message_id=message.message_id,
                question_turn=message.question_turn,
            )
        except ValueError as refused:
            raise HTTPException(status_code=422, detail=str(refused)) from refused

        return message_response(stream)

    def message_response(stream: Any) -> EventSourceResponse:
        async def events() -> AsyncIterator[ServerSentEvent]:
            try:
                async for event in stream:
                    yield ServerSentEvent(data=json.dumps(event), event=str(event["type"]))
            finally:
                await stream.aclose()

        headers = {} if stream.message_id is None else {"X-EKI-Message-ID": str(stream.message_id)}
        return EventSourceResponse(events(), headers=headers)

    @app.get("/sessions/{session_id}/messages")
    async def pending_messages(session_id: UUID, service: Service) -> list[dict[str, Any]]:
        return await service.receipts(session_id)

    @app.get("/sessions/{session_id}/messages/{message_id}")
    async def message_status(
        session_id: UUID, message_id: UUID, service: Service
    ) -> dict[str, Any]:
        return await service.receipt(session_id, message_id)

    @app.post("/sessions/{session_id}/messages/{message_id}/resume")
    async def resume_message(
        session_id: UUID, message_id: UUID, service: Service
    ) -> EventSourceResponse:
        return message_response(await service.resume(session_id, message_id))

    @app.post("/sessions/{session_id}/resume")
    async def resume_session(session_id: UUID, service: Service) -> EventSourceResponse:
        return message_response(await service.resume(session_id))

    @app.get("/runs/{run_id}/access")
    async def read_access(run_id: UUID, request: Request, books: Runbooks) -> dict[str, object]:
        def work() -> dict[str, object]:
            with books._connect() as conn:
                return access.readers(conn, run_id, identity(request))

        return await db(work)

    @app.put("/runs/{run_id}/access")
    async def update_access(
        run_id: UUID, body: Readers, request: Request, books: Runbooks
    ) -> dict[str, object]:
        def work() -> dict[str, object]:
            with books._connect() as conn:
                access.set_readers(conn, run_id, body.readers, identity(request))
                return access.readers(conn, run_id, identity(request))

        return await operation(work)

    @app.put("/runs/{run_id}/owner")
    async def update_owner(
        run_id: UUID, body: Owner, request: Request, books: Runbooks
    ) -> dict[str, object]:
        def work() -> dict[str, object]:
            with books._connect() as conn:
                access.set_owner(conn, run_id, body.subject, identity(request))
                return access.readers(conn, run_id, identity(request))

        return await operation(work)

    @app.post("/runbook/search")
    async def search_entries(
        body: RunbookSearch, request: Request, search: Search
    ) -> dict[str, object]:
        """Search approved entries. The query is in the body so it never appears in a URL."""
        hits = await operation(
            lambda: search.search(identity(request), body.query, limit=body.limit)
        )
        return {"results": [h.public() for h in hits], "embedding_model": search.embedder.name}

    @app.post("/runbook/ask")
    async def ask_runbook(
        body: RunbookQuestion, request: Request, answers: Answers
    ) -> dict[str, Any]:
        try:
            answer = await answers.ask(identity(request), body.question)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="invalid question") from exc
        return answer.model_dump(mode="json")

    def markdown_response(content: str, filename: str) -> Response:
        return Response(
            content,
            media_type="text/markdown",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.get("/entries/{entry_id}/markdown")
    async def export_entry(entry_id: UUID, request: Request, search: Search) -> Response:
        exports = ExportService(search.dsn, search.work_email_domains)
        content = await operation(lambda: exports.entry(identity(request), entry_id))
        return markdown_response(content, f"runbook-{entry_id}.md")

    @app.get("/runbook/export")
    async def export_run(run_id: UUID, request: Request, search: Search) -> Response:
        exports = ExportService(search.dsn, search.work_email_domains)
        content = await operation(lambda: exports.run(identity(request), run_id))
        return markdown_response(content, f"runbook-{run_id}.md")

    @app.get("/runs/{run_id}/audit")
    async def read_audit(
        run_id: UUID, request: Request, search: Search, after: int = 0, limit: int = 100
    ) -> dict[str, object]:
        def read() -> list[dict[str, object]]:
            with psycopg.connect(search.dsn, autocommit=True) as conn:
                if not identity(request).admin:
                    access.require_manager(conn, run_id, identity(request))
                return list_events(conn, run_id, after=after, limit=limit)

        rows = await operation(read)
        return {"events": rows, "next_after": rows[-1]["id"] if rows else after}

    return app


app = create_app()
