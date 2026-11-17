"""What the route modules share: service providers, the database and request helpers."""

from collections.abc import Callable
from dataclasses import dataclass

from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, ValidationError

from eki.api.security import identity
from eki.auth import Authenticator
from eki.graph.workflow import db
from eki.interview.service import InterviewService
from eki.runbook.service import RunbookService
from eki.search.answers import AnswerService
from eki.search.service import SearchService


@dataclass(frozen=True)
class ApiContext:
    """Providers resolved per request, so tests can inject services or use settings."""

    database_dsn: Callable[[], str]
    authenticator: Callable[[], Authenticator]
    service: Callable[[], InterviewService]
    runbooks: Callable[[], RunbookService]
    search: Callable[[], SearchService]
    answers: Callable[[], AnswerService]


class Body(BaseModel):
    """Request bodies refuse unknown fields."""

    model_config = ConfigDict(extra="forbid")


async def operation[T](call: Callable[[], T]) -> T:
    """Run blocking work in a thread; invalid input becomes a 422, never a 500."""
    try:
        return await db(call)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail="invalid runbook change") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def actor(request: Request, name: str) -> None:
    """A reviewer field must name the authenticated identity."""
    if name.strip() != identity(request).name:
        raise HTTPException(
            status_code=422, detail="reviewer must match the authenticated identity"
        )
