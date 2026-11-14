"""Runbook operations shared by HTTP and CLI, independent of model configuration."""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID

import psycopg

from eki.errors import ConflictError, NotFoundError
from eki.ingest.redaction import RedactedText, redact
from eki.interview import store as interviews
from eki.runbook import review, store
from eki.runbook.faithfulness import Unsupported, check_entry
from eki.runbook.models import (
    SECTION_NAMES,
    Claim,
    EntryStatus,
    InterviewTurnCitation,
    RunbookEntry,
    Section,
    SectionName,
)
from eki.runbook.synthesis import build_entry


def validate_sources(conn: psycopg.Connection, session_id: UUID, entry: RunbookEntry) -> None:
    """Every claim must refer to a non-confidential expert turn of the same topic."""
    for section in SECTION_NAMES:
        for claim in entry.section(section).claims:
            if not claim.citations:
                raise ValueError("every claim needs a citation")
            for citation in claim.citations:
                if not isinstance(citation, InterviewTurnCitation) or citation.session_id != str(
                    session_id
                ):
                    raise ValueError("claims must cite this interview")
                turn = interviews.load_turn(conn, session_id, citation.turn_index)
                if (
                    turn is None
                    or turn.speaker != "expert"
                    or turn.kind == "skip"
                    or turn.confidential
                    or turn.topic_id != entry.topic_id
                ):
                    raise ValueError("citation must refer to a public expert turn of this topic")
                if conn.execute(
                    "SELECT 1 FROM turns WHERE session_id = %s AND corrects = %s",
                    (session_id, citation.turn_index),
                ).fetchone():
                    raise ValueError("citation refers to a corrected turn")


def synthesise(
    conn: psycopg.Connection, session_id: UUID, *, overwrite_edits: bool = False
) -> tuple[store.StoredEntry, ...]:
    """Atomically rebuild drafts from the notes. The session lock also serialises reviews.

    Approved and rejected entries are never touched. A draft the expert edited by hand is
    kept as it is, unless ``overwrite_edits`` asks for it to be rebuilt as well.
    """
    with conn.transaction():
        conn.execute("SELECT 1 FROM sessions WHERE id = %s FOR UPDATE", (session_id,))
        session = interviews.load_session(conn, session_id)
        if session is None:
            raise NotFoundError(f"no session {session_id}")
        if session.status != "finished":
            raise ConflictError("the interview must be finished before synthesis")
        plan = interviews.load_plan(conn, session.plan_id)
        if plan is None:
            raise NotFoundError("the session's plan no longer exists")
        notes = interviews.load_notes(conn, session_id)
        opened = interviews.load_open_items(conn, session_id)
        existing = {s.entry.topic_id: s for s in store.list_entries(conn, session_id)}
        for topic in plan.topics:
            previous = existing.get(topic.topic_id)
            if previous is not None and (
                previous.entry.status != EntryStatus.DRAFT
                or (previous.edited and not overwrite_edits)
            ):
                continue
            entry = build_entry(session_id, topic, notes, opened)
            validate_sources(conn, session_id, entry)
            if previous is not None:
                entry = entry.model_copy(update={"id": previous.entry.id})
            store.save_entry(conn, session_id, entry, edited=False)
        return store.list_entries(conn, session_id)


@dataclass
class RunbookService:
    dsn: str = field(repr=False)
    work_email_domains: frozenset[str] = field(default_factory=frozenset)
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)

    def _connect(self) -> psycopg.Connection:
        return psycopg.connect(self.dsn, autocommit=True)

    def synthesise(
        self, session_id: UUID, *, overwrite_edits: bool = False
    ) -> tuple[store.StoredEntry, ...]:
        with self._connect() as conn:
            return synthesise(conn, session_id, overwrite_edits=overwrite_edits)

    def list(self, session_id: UUID) -> tuple[store.StoredEntry, ...]:
        with self._connect() as conn:
            if interviews.load_session(conn, session_id) is None:
                raise NotFoundError(f"no session {session_id}")
            return store.list_entries(conn, session_id)

    def get(self, entry_id: UUID) -> store.StoredEntry:
        with self._connect() as conn:
            stored = store.load_entry(conn, str(entry_id))
            if stored is None:
                raise NotFoundError(f"no entry {entry_id}")
            return stored

    def unsupported(self, session_id: UUID) -> dict[str, tuple[Unsupported, ...]]:
        """Per entry of a session, the claims that say more than the turns they cite.

        For the reviewing side only: the comparison reads the interview transcript.
        """
        with self._connect() as conn:
            if interviews.load_session(conn, session_id) is None:
                raise NotFoundError(f"no session {session_id}")
            turns = interviews.load_turns(conn, session_id)
            return {
                stored.entry.id: check_entry(stored.entry, turns)
                for stored in store.list_entries(conn, session_id)
            }

    def history(self, entry_id: UUID) -> tuple[store.Decision, ...]:
        with self._connect() as conn:
            if store.load_entry(conn, str(entry_id)) is None:
                raise NotFoundError(f"no entry {entry_id}")
            return store.decisions(conn, str(entry_id))

    def _clean(self, text: str) -> RedactedText:
        return redact(text, work_email_domains=self.work_email_domains).text

    def _change(
        self,
        entry_id: UUID,
        by: str,
        action: store.DecisionAction,
        apply: Callable[[psycopg.Connection, store.StoredEntry, str], RunbookEntry],
        *,
        reason: RedactedText | None = None,
    ) -> store.StoredEntry:
        with self._connect() as conn, conn.transaction():
            previous = store.load_entry(conn, str(entry_id))
            if previous is None:
                raise NotFoundError(f"no entry {entry_id}")
            # Lock session first everywhere, including edits, synthesis and corrections.
            conn.execute("SELECT 1 FROM sessions WHERE id = %s FOR UPDATE", (previous.session_id,))
            previous = store.load_entry(conn, str(entry_id), lock=True)
            if previous is None:
                raise NotFoundError(f"no entry {entry_id}")
            row = conn.execute(
                "SELECT r.expert_name FROM ingest_runs r JOIN interview_plans p ON p.run_id = r.id "
                "JOIN sessions s ON s.plan_id = p.id WHERE s.id = %s",
                (previous.session_id,),
            ).fetchone()
            if row is None:
                raise NotFoundError("the entry's session no longer exists")
            review.check_expert(by, row[0])
            changed = apply(conn, previous, row[0])
            validate_sources(conn, previous.session_id, changed)
            result = store.save_entry(
                conn,
                previous.session_id,
                changed,
                rejection_reason=reason,
                edited=True if action == "edit" else None,
            )
            store.add_decision(conn, changed.id, action, self._clean(by), self.clock(), reason)
            return result

    def approve(self, entry_id: UUID, by: str) -> store.StoredEntry:
        return self._change(
            entry_id,
            by,
            "approve",
            lambda conn, previous, expert: review.approve(
                previous.entry, self._clean(by), expert, self.clock()
            ),
        )

    def reject(self, entry_id: UUID, by: str, reason: str) -> store.StoredEntry:
        clean = self._clean(reason)
        return self._change(
            entry_id,
            by,
            "reject",
            lambda conn, previous, expert: review.reject(previous.entry, clean),
            reason=clean,
        )

    def edit(
        self,
        entry_id: UUID,
        by: str,
        sections: dict[SectionName, review.SectionEdit],
    ) -> store.StoredEntry:
        # Validate and redact before any text is stored, even in the transcript.
        if not sections or any(name not in SECTION_NAMES for name in sections):
            raise ValueError("an edit needs named runbook sections")
        clean = {
            name: review.SectionEdit(
                claims=tuple(self._clean(text) for text in section.claims),
                open_question=None
                if section.open_question is None
                else self._clean(section.open_question),
            )
            for name, section in sections.items()
        }
        if any(not s.claims and s.open_question is None for s in clean.values()):
            raise ValueError("an edit cannot leave a section empty")

        def apply(
            conn: psycopg.Connection, previous: store.StoredEntry, expert: str
        ) -> RunbookEntry:
            text = self._clean(
                "\n".join(
                    f"{name}: "
                    + " ".join(
                        (*s.claims, *(() if s.open_question is None else (s.open_question,)))
                    )
                    for name, s in clean.items()
                )
            )
            ordinal = interviews.add_expert_turn(
                conn, previous.session_id, previous.entry.topic_id, "edit", text
            )
            citation = InterviewTurnCitation(
                session_id=str(previous.session_id), turn_index=ordinal
            )
            replacements = {
                name: Section(
                    claims=tuple(Claim(text=t, citations=(citation,)) for t in section.claims),
                    open_question=section.open_question,
                )
                for name, section in clean.items()
            }
            return review.edit(previous.entry, replacements)

        return self._change(entry_id, by, "edit", apply)


def correct_entries(
    conn: psycopg.Connection,
    session_id: UUID,
    corrected_turn: int,
    correction_turn: int,
    text: RedactedText,
    by: RedactedText,
    at: datetime,
) -> None:
    """Update cited text and reopen approval atomically with the transcript correction.

    The caller holds the session row lock, so approval cannot race this operation.
    Sections and claims with unrelated sources remain as the expert reviewed them.
    """
    for previous in store.list_entries(conn, session_id):
        changed: dict[SectionName, Section] = {}
        for name in SECTION_NAMES:
            section = previous.entry.section(name)
            claims: list[Claim] = []
            replaced = False
            for claim in section.claims:
                cites_target = any(
                    isinstance(c, InterviewTurnCitation)
                    and c.session_id == str(session_id)
                    and c.turn_index == corrected_turn
                    for c in claim.citations
                )
                if cites_target:
                    if not replaced:
                        claims.append(
                            Claim(
                                text=text,
                                citations=(
                                    InterviewTurnCitation(
                                        session_id=str(session_id), turn_index=correction_turn
                                    ),
                                ),
                            )
                        )
                    replaced = True
                else:
                    claims.append(claim)
            if replaced:
                changed[name] = Section(claims=tuple(claims), open_question=section.open_question)
        if changed:
            entry = review.edit(previous.entry, changed)
            validate_sources(conn, session_id, entry)
            store.save_entry(conn, session_id, entry)
            store.add_decision(conn, entry.id, "correction", by, at)
