"""F10 review rules, without I/O. Only the expert can approve a complete cited draft."""

from datetime import datetime

from eki.errors import ConflictError
from eki.runbook.models import EntryStatus, RunbookEntry, validate_citations


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
