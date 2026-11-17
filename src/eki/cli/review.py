"""``eki entries`` and ``eki review``: list runbook entries and review drafts."""

import argparse
import json
from collections.abc import Sequence
from datetime import date
from typing import Any
from uuid import UUID

from eki import followup_cli, maintenance
from eki.audit import (
    audit_context,
)
from eki.auth import (
    AuthenticationError,
    TokenAuthenticator,
    acting_as,
    current_principal,
)
from eki.config import Settings, work_domains
from eki.feedback import FeedbackService
from eki.interview import store as interviews
from eki.interview.service import (
    ConflictError,
    NotFoundError,
)
from eki.runbook.faithfulness import Unsupported
from eki.runbook.models import SECTION_NAMES, EntryStatus, InterviewTurnCitation
from eki.runbook.review import SectionEdit
from eki.runbook.service import RunbookService
from eki.runbook.store import StoredEntry


def _show_entry(stored: StoredEntry, unsupported: Sequence[Unsupported] = ()) -> None:
    entry = stored.entry
    doubts = {(u.section, u.index): u.terms for u in unsupported}
    print(f"\n{entry.title} ({entry.status.value}, version {stored.version}) [{entry.id}]")
    for name in SECTION_NAMES:
        section = entry.section(name)
        print(f"  {name}:")
        for index, claim in enumerate(section.claims):
            sources = "; ".join(
                f"interview {c.session_id}, turn {c.turn_index}"
                if isinstance(c, InterviewTurnCitation)
                else f"artifact {c.artifact_id}"
                for c in claim.citations
            )
            print(f"    - {claim.text} [{sources}]")
            if (name, index) in doubts:
                print("      ! not in the cited answer: " + ", ".join(doubts[name, index]))
        if section.open_question is not None:
            print(f"    Open: {section.open_question}")


def _review_entries(
    books: RunbookService,
    session: UUID,
    plan: UUID,
    by: str,
    *,
    include_approved: bool = False,
    entry_ids: set[str] | None = None,
    review_by: date | None = None,
) -> int:
    drafts = [
        e
        for e in books.list(session)
        if (include_approved or e.entry.status == EntryStatus.DRAFT)
        and (entry_ids is None or e.entry.id in entry_ids)
    ]
    if not drafts:
        print("No draft entries to review.")
        return 0
    for stored in drafts:
        entry_id = UUID(stored.entry.id)
        while True:
            stored = books.get(entry_id)
            if not include_approved and stored.entry.status != EntryStatus.DRAFT:
                break
            _show_entry(stored, books.unsupported(session).get(stored.entry.id, ()))
            expected = {
                "expected_version": stored.version,
                "expected_maintenance_revision": books.metadata(entry_id)["maintenance_revision"],
            }
            try:
                action = (
                    input(
                        "Approve, review unchanged, withdraw, reject, edit, triage, skip, or quit? "
                    )
                    .strip()
                    .lower()
                )
                if action in ("quit", "q", "/quit"):
                    raise EOFError
                if action in ("skip", "s"):
                    break
                if action in ("approve", "a"):
                    books.approve(entry_id, by, **expected, review_by=review_by)
                    print("Approved.")
                    break
                if action == "triage":
                    principal = current_principal()
                    if principal is None:
                        raise AuthenticationError("review identity required")
                    feedback = FeedbackService(books.dsn, books.work_email_domains)
                    print(json.dumps(feedback.list(entry_id, principal), default=str))
                    flag_id = UUID(input("Flag ID: ").strip())
                    resolution = input(
                        "Resolution (corrected, confirmed_current, dismissed): "
                    ).strip()
                    feedback.resolve(
                        flag_id,
                        principal,
                        resolution=resolution,
                        note=input("Private resolution reason: "),
                        **expected,
                    )
                    print("Flag resolved.")
                    continue
                if action in ("review", "review unchanged"):
                    books.review(entry_id, by, **expected, review_by=review_by)
                    print("Reviewed; no content changes.")
                    break
                if action == "withdraw":
                    books.withdraw(entry_id, by, input("Reason: "), **expected)
                    print("Withdrawn to draft.")
                    break
                if action in ("reject", "r"):
                    books.reject(entry_id, by, input("Reason: "), **expected)
                    print("Rejected.")
                    break
                if action in ("edit", "e"):
                    name = input("Section: ").strip()
                    if name not in SECTION_NAMES:
                        print("Choose a section: " + ", ".join(SECTION_NAMES))
                        continue
                    claims = []
                    while text := input("Claim (blank to finish): ").strip():
                        claims.append(text)
                    question = input("Open question (blank for none): ").strip() or None
                    books.edit(
                        entry_id,
                        by,
                        {name: SectionEdit(claims=tuple(claims), open_question=question)},
                        **expected,
                    )
                    print("Edited; approval is required.")
                    continue
                print(
                    "Choose approve, review unchanged, withdraw, reject, edit, triage, skip, "
                    "or quit."
                )
            except (EOFError, KeyboardInterrupt):
                print(f"Stopped. Continue with: eki review --plan {plan}")
                return 0
            except (ValueError, ConflictError) as error:
                # Validation can contain rejected input. Never echo that input in the terminal.
                print(
                    str(error)
                    if isinstance(error, ConflictError)
                    else "Review refused: check the name, section and content."
                )
    print("Review complete.")
    return 0


def execute(arguments: argparse.Namespace, settings: Settings) -> int:
    books = RunbookService(settings.database_url.get_secret_value(), work_domains(settings))
    if arguments.command == "review" and arguments.run is not None:
        credential = settings.api_token
        if credential is None:
            raise AuthenticationError("set EKI_API_TOKEN to review entries")
        principal = TokenAuthenticator(settings.api_tokens.get_secret_value()).authenticate(
            "Bearer " + credential.get_secret_value()
        )
        with acting_as(principal), audit_context(user_id=principal.subject):
            selected: list[dict[str, Any]] = []
            offset: int | None = 0
            with books._connect() as conn:
                while offset is not None:
                    page = maintenance.queue(
                        conn,
                        arguments.run,
                        principal,
                        filter="due" if arguments.due else "all",
                        limit=100,
                        offset=offset,
                    )
                    selected.extend(page["entries"])
                    offset = page["next_offset"]
            for sid in dict.fromkeys(e["session_id"] for e in selected):
                with books._connect() as conn:
                    session_row = interviews.load_session(conn, UUID(sid))
                if session_row:
                    _review_entries(
                        books,
                        UUID(sid),
                        session_row.plan_id,
                        principal.name,
                        include_approved=True,
                        entry_ids={e["id"] for e in selected if e["session_id"] == sid},
                        review_by=arguments.review_by,
                    )
        return 0
    with books._connect() as conn:
        plan = interviews.load_plan(conn, arguments.plan)
        if plan is None:
            raise NotFoundError(f"no plan {arguments.plan}")
        session = interviews.session_of_plan(conn, plan.id)
        if session is None:
            raise ConflictError("approve the plan and finish its interview first")
        row = conn.execute(
            "SELECT expert_name FROM ingest_runs WHERE id = %s", (plan.run_id,)
        ).fetchone()
        assert row is not None  # noqa: S101 - plan references this run
    if plan.kind == "follow_up":
        principal = followup_cli.principal(settings)
        followup_cli.require_manager(settings, principal, "plans", plan.id)
        print(f"Mapped proposals: eki follow-up proposals --session {session}")
    if arguments.command == "entries":
        entries = books.list(session)
        unsupported = books.unsupported(session)
        for stored in entries:
            entry = stored.entry
            doubts = len(unsupported.get(entry.id, ()))
            marker = f"  ({doubts} claim{'s' if doubts > 1 else ''} to check)" if doubts else ""
            marker += "  (edited by hand)" if stored.edited else ""
            print(f"{entry.id}  {entry.status.value:8}  {entry.title}{marker}")
        if not entries:
            print(
                "No new-topic draft entries; mapped changes are reviewed through proposals."
                if plan.kind == "follow_up"
                else "No entries yet. Finish the interview first."
            )
        return 0
    credential = settings.api_token
    if credential is None:
        raise AuthenticationError("set EKI_API_TOKEN to review entries")
    principal = TokenAuthenticator(settings.api_tokens.get_secret_value()).authenticate(
        "Bearer " + credential.get_secret_value()
    )
    with acting_as(principal), audit_context(user_id=principal.subject):
        with books._connect() as conn:
            maintenance.reviewer(conn, plan.run_id, principal.name)
        if arguments.review_as and arguments.review_as != principal.name:
            raise ValueError("--review-as cannot impersonate another reviewer")
        return _review_entries(
            books, session, plan.id, principal.name, review_by=arguments.review_by
        )
