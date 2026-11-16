"""Runbook operations shared by HTTP and CLI, independent of model configuration."""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID

import psycopg

from eki import maintenance
from eki.auth import current_principal
from eki.errors import ConflictError, NotFoundError
from eki.followup import proposals
from eki.ingest.redaction import RedactedText, redact
from eki.interview import store as interviews
from eki.interview.models import Turn
from eki.runbook import maintenance as review_metadata
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
from eki.runbook.steps import claim_texts
from eki.runbook.synthesis import build_entry


def validate_sources(conn: psycopg.Connection, session_id: UUID, entry: RunbookEntry) -> None:
    """Every claim must refer to a non-confidential expert turn of the same topic.

    ``session_id`` is the entry's own (home) interview. A follow-up session counts as a
    source only when a proposal explicitly links it to this entry and topic (F22 P5).
    """
    validate_claims(conn, session_id, entry)


def validate_claims(conn: psycopg.Connection, home: UUID, entry: RunbookEntry) -> None:
    """Check each citation against the session it cites: home or an explicitly linked one.

    A linked follow-up can only supply the expert's own answers and corrections; its
    edit turns, skips, confidential answers and interviewer turns never qualify.
    """
    wrong_session = "claims must cite this interview or a linked follow-up"
    allowed = {str(home)} | {
        str(s) for s in proposals.linked_sessions(conn, entry.id, entry.topic_id)
    }
    turns: dict[str, dict[int, Turn]] = {}
    corrected: dict[str, set[int]] = {}
    for section in SECTION_NAMES:
        for claim in entry.section(section).claims:
            if not claim.citations:
                raise ValueError("every claim needs a citation")
            for citation in claim.citations:
                if not isinstance(citation, InterviewTurnCitation):
                    raise ValueError(wrong_session)
                try:
                    cited = str(UUID(citation.session_id))
                except ValueError:
                    raise ValueError(wrong_session) from None
                if cited not in allowed:
                    raise ValueError(wrong_session)
                if cited not in turns:
                    turns[cited] = {t.ordinal: t for t in interviews.load_turns(conn, UUID(cited))}
                    corrected[cited] = {
                        int(r[0])
                        for r in conn.execute(
                            "SELECT corrects FROM turns WHERE session_id = %s "
                            "AND corrects IS NOT NULL",
                            (UUID(cited),),
                        ).fetchall()
                    }
                turn = turns[cited].get(citation.turn_index)
                if (
                    turn is None
                    or turn.speaker not in ("expert", "maintainer")
                    or turn.kind == "skip"
                    or turn.confidential
                    or turn.topic_id != entry.topic_id
                ):
                    raise ValueError("citation must refer to a public expert turn of this topic")
                if cited != str(home) and (
                    turn.speaker != "expert" or turn.kind not in ("answer", "correction")
                ):
                    raise ValueError("a linked follow-up citation must be the expert's answer")
                if turn.speaker == "maintainer" and (
                    citation.author_subject != turn.author_subject
                    or citation.author_name != turn.author_name
                ):
                    raise ValueError("maintainer citation attribution does not match its source")
                if citation.turn_index in corrected[cited]:
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
            # Existing targets go through the F22 proposal/apply path (9c). Never create
            # a second entry for them through either automatic or explicit synthesis.
            if plan.kind == "follow_up" and topic.target_entry_id is not None:
                continue
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
            if previous is not None and previous.entry == entry and not previous.edited:
                continue
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
            run_id = maintenance_run(conn, session_id)
            maintenance.capture(conn, run_id)
            result = synthesise(conn, session_id, overwrite_edits=overwrite_edits)
            self._backfill_proposals(conn, run_id, session_id)
            return result

    def _backfill_proposals(self, conn: psycopg.Connection, run_id: UUID, session_id: UUID) -> None:
        """Explicit, idempotent proposal creation for a follow-up finished before 9c.

        Capture mode only (the caller checked); finishing already creates them otherwise.
        """
        from eki.followup.sessions import outcome

        with conn.transaction():
            conn.execute("SELECT 1 FROM sessions WHERE id = %s FOR UPDATE", (session_id,))
            session = interviews.load_session(conn, session_id)
            if session is None or session.kind != "follow_up" or session.status != "finished":
                return
            plan = interviews.load_plan(conn, session.plan_id)
            if plan is None:
                return
            outcomes = {
                str(q.gap_id): outcome(conn, session_id, str(q.gap_id))[0]
                for topic in plan.topics
                for q in topic.questions
                if q.gap_id is not None
            }
            created = proposals.ensure(conn, session_id, outcomes, now=self.clock())
            proposals.audit_proposed(conn, run_id, created)

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
        expected_version: int | None = None,
        expected_maintenance_revision: int | None = None,
        review_by: date | None = None,
    ) -> store.StoredEntry:
        with self._connect() as conn, conn.transaction():
            previous = store.load_entry(conn, str(entry_id))
            if previous is None:
                raise NotFoundError(f"no entry {entry_id}")
            run_id = maintenance_run(conn, previous.session_id)
            expert, maintaining = maintenance.reviewer(conn, run_id, by)
            # Acquire run before session and entry for every maintenance mutation.
            conn.execute("SELECT 1 FROM sessions WHERE id = %s FOR UPDATE", (previous.session_id,))
            previous = store.load_entry(conn, str(entry_id), lock=True)
            if previous is None:
                raise NotFoundError(f"no entry {entry_id}")
            review_metadata.expected(
                conn,
                entry_id,
                previous.version,
                expected_version,
                expected_maintenance_revision,
                required=maintaining,
            )
            if action == "approve":
                review_metadata.deadline(self.clock(), review_by)
            changed = apply(conn, previous, expert)
            validate_sources(conn, previous.session_id, changed)
            result = store.save_entry(
                conn,
                previous.session_id,
                changed,
                rejection_reason=reason,
                edited=True if action == "edit" else None,
            )
            if action == "approve":
                review_metadata.reviewed(conn, entry_id, self._clean(by), self.clock(), review_by)
            store.add_decision(conn, changed.id, action, self._clean(by), self.clock(), reason)
            return result

    def approve(
        self,
        entry_id: UUID,
        by: str,
        *,
        expected_version: int | None = None,
        expected_maintenance_revision: int | None = None,
        review_by: date | None = None,
    ) -> store.StoredEntry:
        return self._change(
            entry_id,
            by,
            "approve",
            expected_version=expected_version,
            expected_maintenance_revision=expected_maintenance_revision,
            review_by=review_by,
            apply=lambda conn, previous, expert: review.approve(
                previous.entry, self._clean(by), expert, self.clock()
            ),
        )

    def reject(
        self,
        entry_id: UUID,
        by: str,
        reason: str,
        *,
        expected_version: int | None = None,
        expected_maintenance_revision: int | None = None,
    ) -> store.StoredEntry:
        clean = self._clean(reason)
        return self._change(
            entry_id,
            by,
            "reject",
            lambda conn, previous, expert: review.reject(previous.entry, clean),
            reason=clean,
            expected_version=expected_version,
            expected_maintenance_revision=expected_maintenance_revision,
        )

    def edit(
        self,
        entry_id: UUID,
        by: str,
        sections: dict[SectionName, review.SectionEdit],
        *,
        expected_version: int | None = None,
        expected_maintenance_revision: int | None = None,
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
            principal = current_principal()
            mode = conn.execute(
                "SELECT maintenance_started_at FROM ingest_runs WHERE id=%s",
                (maintenance_run(conn, previous.session_id),),
            ).fetchone()
            if mode and mode[0] and principal:
                conn.execute(
                    "UPDATE turns SET speaker='maintainer',author_subject=%s,author_name=%s "
                    "WHERE session_id=%s AND ordinal=%s",
                    (principal.subject, self._clean(principal.name), previous.session_id, ordinal),
                )
            citation = InterviewTurnCitation(
                session_id=str(previous.session_id),
                turn_index=ordinal,
                author_subject=principal.subject if mode and mode[0] and principal else None,
                author_name=self._clean(principal.name) if mode and mode[0] and principal else None,
            )
            replacements = {
                name: Section(
                    claims=tuple(Claim(text=t, citations=(citation,)) for t in section.claims),
                    open_question=section.open_question,
                )
                for name, section in clean.items()
            }
            return review.edit(previous.entry, replacements)

        return self._change(
            entry_id,
            by,
            "edit",
            apply,
            expected_version=expected_version,
            expected_maintenance_revision=expected_maintenance_revision,
        )

    def metadata(self, entry_id: UUID) -> dict[str, Any]:
        with self._connect() as conn:
            return review_metadata.info(conn, entry_id, self.clock())

    def review(
        self,
        entry_id: UUID,
        by: str,
        *,
        expected_version: int,
        expected_maintenance_revision: int,
        review_by: date | None = None,
    ) -> store.StoredEntry:
        with self._connect() as conn, conn.transaction():
            entry = store.load_entry(conn, str(entry_id))
            if entry is None:
                raise NotFoundError("entry not found")
            maintenance.reviewer(conn, maintenance_run(conn, entry.session_id), by)
            conn.execute("SELECT 1 FROM sessions WHERE id=%s FOR UPDATE", (entry.session_id,))
            entry = store.load_entry(conn, str(entry_id), lock=True)
            if entry is None or entry.entry.status != EntryStatus.APPROVED:
                raise ConflictError("only approved entries can be reviewed unchanged")
            review_metadata.expected(
                conn,
                entry_id,
                entry.version,
                expected_version,
                expected_maintenance_revision,
                required=True,
            )
            if entry.entry.missing_sections():
                raise ValueError("entry has missing sections")
            validate_sources(conn, entry.session_id, entry.entry)
            review_metadata.reviewed(conn, entry_id, self._clean(by), self.clock(), review_by)
            store.add_decision(conn, str(entry_id), "review", self._clean(by), self.clock())
            return entry

    def withdraw(
        self,
        entry_id: UUID,
        by: str,
        reason: str,
        *,
        expected_version: int,
        expected_maintenance_revision: int,
    ) -> store.StoredEntry:
        if not reason.strip() or len(reason) > 2000:
            raise ValueError("withdrawal needs a reason")

        def apply(
            conn: psycopg.Connection, previous: store.StoredEntry, expert: str
        ) -> RunbookEntry:
            if previous.entry.status != EntryStatus.APPROVED:
                raise ConflictError("only an approved entry can be withdrawn")
            return previous.entry.model_copy(
                update={"status": EntryStatus.DRAFT, "approved_by": None, "approved_at": None}
            )

        return self._change(
            entry_id,
            by,
            "withdraw",
            apply,
            reason=self._clean(reason),
            expected_version=expected_version,
            expected_maintenance_revision=expected_maintenance_revision,
        )


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
                        claims.extend(
                            Claim(
                                text=step,
                                citations=(
                                    InterviewTurnCitation(
                                        session_id=str(session_id), turn_index=correction_turn
                                    ),
                                ),
                            )
                            for step in claim_texts(name, text)
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


def maintenance_run(conn: psycopg.Connection, session_id: UUID) -> UUID:
    row = conn.execute(
        "SELECT p.run_id FROM sessions s JOIN interview_plans p ON p.id=s.plan_id WHERE s.id=%s",
        (session_id,),
    ).fetchone()
    if row is None:
        raise NotFoundError("session not found")
    return UUID(str(row[0]))
