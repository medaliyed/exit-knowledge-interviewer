"""Small approved runbooks with known relevance, plus draft and private distractors."""

from dataclasses import dataclass
from uuid import UUID

from eki import access
from eki.auth import Principal
from eki.ingest.embedding import HashingEmbedder
from eki.interview import store as interviews
from eki.runbook import store
from eki.runbook.models import SECTION_NAMES
from eki.runbook.review import SectionEdit
from eki.runbook.service import RunbookService
from eki.search.embedding import SearchEmbedder
from eki.search.jobs import IndexWorker
from tests.conftest import Scratch
from tests.test_workflow_synthesis import finished

OWNER = Principal("owner", "Nadia Ferrand")
READER = Principal("reader", "Reader")
STRANGER = Principal("stranger", "Stranger")
ADMIN = Principal("admin", "Operator", True)


@dataclass
class SearchWorld:
    run_id: UUID
    session_id: UUID
    billing: UUID
    gateway: UUID
    draft: UUID


def index_runbook(scratch: Scratch, embedder: SearchEmbedder | None = None) -> dict[str, int]:
    return IndexWorker(scratch.dsn, embedder or HashingEmbedder()).drain()


async def search_world(scratch: Scratch, *, indexed: bool = True) -> SearchWorld:
    session = await finished(scratch)
    books = RunbookService(scratch.dsn)
    entries = books.list(session)
    titles = (
        "Billing month-end reconciliation",
        "Gateway certificate renewal",
        "Unapproved payroll procedure",
    )
    texts = (
        {
            "trigger": "Month-end totals differ between billing-batch and ledger-db.",
            "diagnosis": "Compare the last posting sequence number on both sides.",
            "steps": (
                "Re-run the reconciliation job with the replay flag for the last day of the month."
            ),
            "rollback": (
                "Restore the reconciliation table from the pre-run export; the job is idempotent."
            ),
            "contacts": "Finance operations confirm the totals.",
            "pitfalls": "Run reconciliation before the period is closed.",
            "rationale": "Late postings land after the month-end cut-off.",
        },
        {s: "Renew the gateway certificate after vendor confirmation." for s in SECTION_NAMES},
        {s: "Payroll reconciliation draft secret procedure." for s in SECTION_NAMES},
    )
    for n, previous in enumerate(entries):
        entry_id = UUID(previous.entry.id)
        edited = books.edit(
            entry_id, OWNER.name, {s: SectionEdit(claims=(texts[n][s],)) for s in SECTION_NAMES}
        )
        with scratch.connect() as conn:
            store.save_entry(conn, session, edited.entry.model_copy(update={"title": titles[n]}))
        if n < 2:
            books.approve(entry_id, OWNER.name)
    with scratch.connect() as conn:
        view = interviews.load_session(conn, session)
        assert view is not None
        access.set_owner(conn, view.run_id, OWNER.subject, ADMIN)
        access.set_readers(conn, view.run_id, [READER.subject], OWNER)
    if indexed:
        index_runbook(scratch)
    return SearchWorld(view.run_id, session, *(UUID(e.entry.id) for e in entries))
