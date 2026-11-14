"""F10 review rules, without I/O. Only the expert can approve a complete cited draft."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from eki.errors import ConflictError
from eki.runbook.models import (
    EntryStatus,
    NonBlank,
    RunbookEntry,
    Section,
    SectionName,
    validate_citations,
)


def check_expert(by: str, expert: str) -> None:
    if not by.strip() or by.strip() != expert:
        raise ValueError("reviewer must match the run's expert")


def _draft(entry: RunbookEntry) -> None:
    if entry.status != EntryStatus.DRAFT:
        raise ConflictError("only a draft can be approved or rejected; edit it first")


def approve(entry: RunbookEntry, by: str, expert: str, at: datetime) -> RunbookEntry:
    _draft(entry)
    check_expert(by, expert)
    if entry.missing_sections():
        raise ValueError("entry has missing sections")
    validate_citations(entry)
    return entry.model_copy(
        update={"status": EntryStatus.APPROVED, "approved_by": by.strip(), "approved_at": at}
    )


def reject(entry: RunbookEntry, reason: str) -> RunbookEntry:
    _draft(entry)
    if not reason.strip():
        raise ValueError("rejection needs a reason")
    return entry.model_copy(update={"status": EntryStatus.REJECTED})


class SectionEdit(BaseModel):
    """Replace the claims and/or open question of one section, in the expert's words."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    claims: tuple[NonBlank, ...] = ()
    open_question: NonBlank | None = None


def edit(entry: RunbookEntry, sections: dict[SectionName, Section]) -> RunbookEntry:
    if not sections or any(not s.claims and s.open_question is None for s in sections.values()):
        raise ValueError("an edit must supply complete sections")
    return entry.model_copy(
        update={
            **{str(name): section for name, section in sections.items()},
            "status": EntryStatus.DRAFT,
            "approved_by": None,
            "approved_at": None,
        }
    )
