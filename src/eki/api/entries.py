"""Private runbook entry review and follow-up proposals (F9, F10, F21, F22)."""

from collections.abc import Sequence
from dataclasses import asdict
from datetime import date
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import Depends, FastAPI, Request
from pydantic import Field

from eki.api.context import ApiContext, Body, actor, operation
from eki.api.security import identity
from eki.graph.workflow import db
from eki.runbook.faithfulness import Unsupported
from eki.runbook.models import SECTION_NAMES, SectionName
from eki.runbook.review import SectionEdit
from eki.runbook.service import RunbookService
from eki.runbook.store import StoredEntry


class Approval(Body):
    approved_by: str
    expected_version: int | None = None
    expected_maintenance_revision: int | None = None
    review_by: date | None = None


class EntryEdit(Body):
    expected_version: int | None = None
    expected_maintenance_revision: int | None = None
    edited_by: str
    sections: dict[SectionName, SectionEdit]


class Rejection(Body):
    expected_version: int | None = None
    expected_maintenance_revision: int | None = None
    rejected_by: str
    reason: str


class ProposalApply(Body):
    """Apply the chosen sections of a follow-up proposal: merge adds, replace swaps claims."""

    applied_by: str
    sections: dict[SectionName, Literal["merge", "replace"]] = Field(min_length=1)
    expected_revision: int = Field(ge=1)
    expected_version: int = Field(ge=1)
    expected_maintenance_revision: int | None = None


class ProposalRefresh(Body):
    refreshed_by: str
    expected_revision: int = Field(ge=1)
    expected_version: int = Field(ge=1)


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


def install_entries(app: FastAPI, ctx: ApiContext) -> None:
    Runbooks = Annotated[RunbookService, Depends(ctx.runbooks)]

    def summaries(books: RunbookService, session_id: UUID) -> list[dict[str, Any]]:
        unsupported = books.unsupported(session_id)
        return [
            {
                **_entry_summary(e, unsupported.get(e.entry.id, ())),
                **books.metadata(UUID(e.entry.id)),
            }
            for e in books.list(session_id)
        ]

    async def detail(books: RunbookService, entry_id: UUID) -> dict[str, Any]:
        def work() -> dict[str, Any]:
            stored = books.get(entry_id)
            unsupported = books.unsupported(stored.session_id).get(stored.entry.id, ())
            return {
                **stored.entry.model_dump(mode="json"),
                **books.metadata(entry_id),
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
            return await db(books.reader_view, entry_id, identity(request))
        return await detail(books, entry_id)

    @app.patch("/entries/{entry_id}")
    async def edit_entry(
        entry_id: UUID, edit: EntryEdit, books: Runbooks, request: Request
    ) -> dict[str, Any]:
        actor(request, edit.edited_by)
        await operation(
            lambda: books.edit(
                entry_id,
                edit.edited_by,
                edit.sections,
                expected_version=edit.expected_version,
                expected_maintenance_revision=edit.expected_maintenance_revision,
            )
        )
        return await detail(books, entry_id)

    @app.post("/entries/{entry_id}/approve")
    async def approve_entry(
        entry_id: UUID, approval: Approval, books: Runbooks, request: Request
    ) -> dict[str, Any]:
        actor(request, approval.approved_by)
        await operation(
            lambda: books.approve(
                entry_id,
                approval.approved_by,
                expected_version=approval.expected_version,
                expected_maintenance_revision=approval.expected_maintenance_revision,
                review_by=approval.review_by,
            )
        )
        return await detail(books, entry_id)

    @app.post("/entries/{entry_id}/reject")
    async def reject_entry(
        entry_id: UUID, rejection: Rejection, books: Runbooks, request: Request
    ) -> dict[str, Any]:
        actor(request, rejection.rejected_by)
        await operation(
            lambda: books.reject(
                entry_id,
                rejection.rejected_by,
                rejection.reason,
                expected_version=rejection.expected_version,
                expected_maintenance_revision=rejection.expected_maintenance_revision,
            )
        )
        return await detail(books, entry_id)

    @app.get("/sessions/{session_id}/follow-up-proposals")
    async def follow_up_proposals(session_id: UUID, books: Runbooks) -> dict[str, Any]:
        """Private comparisons of the entry proposals a finished follow-up produced (F22)."""
        found = await operation(lambda: books.proposals_for_session(session_id))
        return {"session_id": str(session_id), "proposals": found}

    @app.post("/follow-up-proposals/{proposal_id}/apply")
    async def apply_proposal(
        proposal_id: UUID, body: ProposalApply, books: Runbooks, request: Request
    ) -> dict[str, Any]:
        """Apply selected sections as a new draft; publication still needs approval."""
        actor(request, body.applied_by)
        stored, replayed = await operation(
            lambda: books.apply_proposal(
                proposal_id,
                body.applied_by,
                body.sections,
                expected_revision=body.expected_revision,
                expected_version=body.expected_version,
                expected_maintenance_revision=body.expected_maintenance_revision,
            )
        )
        return {
            "entry": await detail(books, UUID(stored.entry.id)),
            "proposal": await operation(lambda: books.proposal(proposal_id)),
            "replayed": replayed,
        }

    @app.post("/follow-up-proposals/{proposal_id}/refresh")
    async def refresh_proposal(
        proposal_id: UUID, body: ProposalRefresh, books: Runbooks, request: Request
    ) -> dict[str, Any]:
        """Rebase a pending proposal on the entry version the owner has now reviewed."""
        actor(request, body.refreshed_by)
        return await operation(
            lambda: books.refresh_proposal(
                proposal_id,
                body.refreshed_by,
                expected_revision=body.expected_revision,
                expected_version=body.expected_version,
            )
        )
