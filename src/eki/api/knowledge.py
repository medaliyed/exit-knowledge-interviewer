"""Approved-knowledge search, cited answers and Markdown export (F11, F12)."""

from typing import Annotated, Any
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import Response

from eki.api.context import ApiContext, Body, operation
from eki.api.security import identity
from eki.runbook.export import ExportService
from eki.search.answers import AnswerService
from eki.search.service import SearchService


class RunbookQuestion(Body):
    question: str


class RunbookSearch(Body):
    query: str
    limit: int = 5


def install_knowledge(app: FastAPI, ctx: ApiContext) -> None:
    Search = Annotated[SearchService, Depends(ctx.search)]
    Answers = Annotated[AnswerService, Depends(ctx.answers)]

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
