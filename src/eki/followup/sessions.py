"""F22 follow-up sessions: deterministic plans, respondent authority and durable budgets.

No model is called here. Lock order: run, gap rows (stable id order), plan.
"""

import hashlib
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

import psycopg
from psycopg import sql

from eki import maintenance
from eki.audit import AuditDetails, AuditError, AuditRecorder, Kind, Phase, append_event
from eki.auth import AuthenticationError, Principal, acting_as
from eki.errors import ConflictError, NotFoundError
from eki.followup import gaps as gap_store
from eki.followup import proposals
from eki.ingest.redaction import RedactedText, redact
from eki.interview import delivery, store
from eki.interview.models import (
    SECTIONS,
    FollowUpLimits,
    InterviewPlan,
    PlannedTopic,
    Question,
)
from eki.llm import ModelError
from eki.runbook.models import RunbookEntry, SectionName
from eki.runbook.service import synthesise

DEFAULT_LIMITS = FollowUpLimits()
CompletionReason = Literal["complete", "question_limit", "model_budget", "expert_stopped"]
Counter = Literal["model_calls", "embedding_calls"]
STOPPED = RedactedText("the expert stopped the follow-up")


def respondent(
    conn: psycopg.Connection, run_id: UUID, principal: Principal | None, by: str | None = None
) -> str:
    """The original expert, signed in as the run's current owner, is the only respondent.

    A same-name account, an administrator who is not the owner, or a reader is refused; so
    is any follow-up capture once maintenance has started.
    """
    if principal is None:
        raise AuthenticationError("the original expert must be signed in to run a follow-up")
    maintenance.active(conn, principal)
    owner, started, _, expert = maintenance.lock_run(conn, run_id)
    if started:
        raise ConflictError("capture is closed after maintenance handover")
    if by is not None and by.strip() != principal.name:
        raise ValueError("approval must match the authenticated identity")
    if principal.subject != owner or principal.name != expert:
        raise NotFoundError("plan not found")
    return principal.subject


def usage_counts(conn: psycopg.Connection, session_id: UUID) -> dict[str, int]:
    row = conn.execute(
        "SELECT (SELECT count(*) FROM turns WHERE session_id = %s AND kind = 'question'), "
        "coalesce(u.model_calls, 0), coalesce(u.embedding_calls, 0) FROM sessions s "
        "LEFT JOIN session_usage u ON u.session_id = s.id WHERE s.id = %s",
        (session_id, session_id),
    ).fetchone()
    if row is None:
        raise NotFoundError("session not found")
    return {"questions": int(row[0]), "model_calls": int(row[1]), "embedding_calls": int(row[2])}


class ProviderBudgetExceeded(ModelError):
    """No provider work began because its durable follow-up allowance was spent."""


def reserve(
    conn: psycopg.Connection, session_id: UUID, counter: Counter, call_id: UUID, limit: int
) -> None:
    """Charge one provider attempt durably before it starts; a crashed attempt stays charged."""
    column = sql.Identifier(counter)
    if conn.execute(
        "SELECT 1 FROM session_provider_attempts WHERE call_id=%s", (call_id,)
    ).fetchone():
        return
    conn.execute(
        "INSERT INTO session_usage(session_id) VALUES (%s) ON CONFLICT DO NOTHING", (session_id,)
    )
    row = conn.execute(
        sql.SQL(
            "UPDATE session_usage SET {c}={c}+1 WHERE session_id=%s AND {c}<%s RETURNING session_id"
        ).format(c=column),
        (session_id, limit),
    ).fetchone()
    if row is None:
        raise ProviderBudgetExceeded("follow-up provider allowance exhausted")
    conn.execute(
        "INSERT INTO session_provider_attempts(call_id,session_id,counter) VALUES (%s,%s,%s)",
        (call_id, session_id, counter),
    )


@dataclass(repr=False)
class FollowUpRecorder(AuditRecorder):
    """Reserve budget and persist the matching audit start in a single transaction."""

    session_id: UUID
    limits: FollowUpLimits

    def record(
        self,
        kind: Kind,
        phase: Phase,
        details: AuditDetails,
        *,
        call_id: UUID,
        run_ids: tuple[UUID, ...] | None = None,
    ) -> None:
        if kind != "model":
            return super().record(kind, phase, details, call_id=call_id, run_ids=run_ids)
        try:
            with psycopg.connect(self.dsn, autocommit=True) as conn, conn.transaction():
                session = store.load_session(conn, self.session_id)
                if session is None:
                    raise NotFoundError("session not found")
                if phase == "started":
                    counter: Counter = (
                        "embedding_calls" if details.operation == "embedding" else "model_calls"
                    )
                    reserve(conn, self.session_id, counter, call_id, getattr(self.limits, counter))
                append_event(
                    conn,
                    kind,
                    phase,
                    details.model_copy(update={"target_id": self.session_id}),
                    run_id=session.run_id,
                    call_id=call_id,
                )
        except psycopg.Error:
            raise AuditError("audit storage unavailable; operation refused") from None


def set_completion_reason(
    conn: psycopg.Connection, session_id: UUID, reason: CompletionReason
) -> None:
    conn.execute(
        "UPDATE sessions SET completion_reason = coalesce(completion_reason, %s) WHERE id = %s",
        (reason, session_id),
    )


def exhausted(
    conn: psycopg.Connection, session_id: UUID, limits: FollowUpLimits
) -> CompletionReason | None:
    counts = usage_counts(conn, session_id)
    if counts["model_calls"] >= limits.model_calls:
        return "model_budget"
    if counts["questions"] >= limits.questions:
        return "question_limit"
    return None


def usage(conn: psycopg.Connection, session_id: UUID) -> dict[str, Any]:
    row = conn.execute(
        "SELECT p.kind, p.limits, s.completion_reason, s.status, "
        "extract(epoch FROM (coalesce(s.finished_at,now())-s.created_at)) FROM sessions s "
        "JOIN interview_plans p ON p.id = s.plan_id WHERE s.id = %s",
        (session_id,),
    ).fetchone()
    if row is None:
        raise NotFoundError("session not found")
    tokens = conn.execute(
        "SELECT sum((details->>'input_tokens')::bigint), "
        "sum((details->>'output_tokens')::bigint) FROM audit_events WHERE "
        "kind='model' AND phase='completed' AND details->>'target_id'=%s "
        "AND details->>'operation'='interview'",
        (str(session_id),),
    ).fetchone()
    partial = conn.execute(
        "SELECT EXISTS(SELECT 1 FROM follow_up_attempts WHERE session_id=%s AND outcome "
        "<>'answered') OR EXISTS(SELECT 1 FROM open_items WHERE session_id=%s) OR "
        "EXISTS(SELECT 1 FROM notes WHERE session_id=%s AND confidential AND NOT superseded)",
        (session_id, session_id, session_id),
    ).fetchone()
    return {
        **usage_counts(conn, session_id),
        "kind": row[0],
        "limits": row[1],
        "completion_reason": row[2],
        "status": row[3],
        "duration_seconds": float(row[4]),
        "input_tokens": tokens[0] if tokens else None,
        "output_tokens": tokens[1] if tokens else None,
        "partial": row[3] == "finished" and (row[2] != "complete" or bool(partial and partial[0])),
    }


def outcome(conn: psycopg.Connection, session_id: UUID, gap_id: str) -> tuple[str, int | None]:
    """What happened to one seed: derived from its attributed questions and their answers."""
    rows = conn.execute(
        "SELECT q.ordinal, a.kind, a.confidential, a.ordinal FROM turns q "
        "LEFT JOIN turns a ON a.session_id = q.session_id AND a.answers = q.ordinal "
        "WHERE q.session_id = %s AND q.gap_id = %s AND q.kind = 'question' ORDER BY q.ordinal",
        (session_id, UUID(gap_id)),
    ).fetchall()
    if not rows:
        return "unattempted", None
    first = int(rows[0][0])
    public_answers = [r[3] for r in rows if r[1] == "answer" and not r[2]]
    if public_answers:
        noted = conn.execute(
            "SELECT 1 FROM notes WHERE session_id = %s AND NOT superseded AND NOT confidential "
            "AND turn_ordinal = ANY(%s) LIMIT 1",
            (session_id, public_answers),
        ).fetchone()
        if noted:
            return "answered", first
        unknown = conn.execute(
            "SELECT 1 FROM turns WHERE session_id=%s AND ordinal=ANY(%s) "
            "AND follow_up_outcome='unknown'",
            (session_id, public_answers),
        ).fetchone()
        return ("unknown" if unknown else "answered_unnoted"), first
    if any(r[1] == "answer" and r[2] for r in rows):
        return "confidential", first
    if any(r[1] == "skip" for r in rows):
        return "skipped", first
    return "unattempted", first


def finish(
    conn: psycopg.Connection,
    session_id: UUID,
    reason: CompletionReason | None = None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Finish a follow-up: completion reason, one outcome per seed and gap statuses.

    Answered seeds await review; everything else returns to triage with its attempt
    recorded. Safe to repeat.
    """
    with conn.transaction():
        session = store.load_session(conn, session_id)
        if session is None:
            raise NotFoundError("session not found")
        plan = store.load_plan(conn, session.plan_id)
        if plan is None:
            raise NotFoundError("plan not found")
        if session.status == "finished":
            return usage(conn, session_id)
        completion = session.completion_reason or reason or "complete"
        for topic in plan.topics:
            checklist = store.checklist(conn, session_id, topic.topic_id)
            for section in topic.sections or SECTIONS:
                if checklist[section] == "missing":
                    store.mark_open(
                        conn,
                        session_id,
                        topic.topic_id,
                        section,
                        RedactedText("not captured before follow-up ended: " + completion),
                        turn=None,
                    )
        store.finish_session(conn, session_id)
        set_completion_reason(conn, session_id, reason or "complete")
        synthesise(conn, session_id)
        now = now or datetime.now(UTC)
        seeds = [q for topic in plan.topics for q in topic.questions if q.gap_id is not None]
        # Pure reads first; then rows are written in the global lock order (session,
        # entries via proposal foreign keys, gaps) and the audit lock is taken last.
        results = {str(q.gap_id): outcome(conn, session_id, str(q.gap_id)) for q in seeds}
        created = proposals.ensure(conn, session_id, {g: r[0] for g, r in results.items()}, now=now)
        for gap_id, (result, turn) in results.items():
            conn.execute(
                "INSERT INTO follow_up_attempts (gap_id, session_id, outcome, question_turn, "
                "recorded_at) VALUES (%s, %s, %s, %s, %s) ON CONFLICT (gap_id, session_id) "
                "DO UPDATE SET outcome = EXCLUDED.outcome, "
                "question_turn = EXCLUDED.question_turn",
                (UUID(gap_id), session_id, result, turn, now),
            )
            status = "awaiting_review" if result == "answered" else "pending"
            conn.execute(
                "UPDATE follow_up_gaps SET status = %s, revision = revision + 1, "
                "plan_id = CASE WHEN %s = 'awaiting_review' THEN plan_id ELSE NULL END "
                "WHERE (id = %s OR group_id = %s) AND status = 'scheduled'",
                (status, status, UUID(gap_id), UUID(gap_id)),
            )
        proposals.audit_proposed(conn, session.run_id, created)
        append_event(
            conn,
            "review",
            "completed",
            AuditDetails(operation="follow_up_finished", target_id=session_id),
            run_id=session.run_id,
        )
    return usage(conn, session_id)


def release(conn: psycopg.Connection, before: InterviewPlan, after: InterviewPlan) -> None:
    """Return the gaps of removed follow-up topics to triage when a draft plan is edited."""
    if before.kind != "follow_up":
        return
    kept = {q.gap_id for t in after.topics for q in t.questions if q.gap_id}
    released = [
        UUID(g) for t in before.topics for q in t.questions if (g := q.gap_id) and g not in kept
    ]
    if released:
        conn.execute(
            "UPDATE follow_up_gaps SET status = 'pending', plan_id = NULL, revision = revision + 1 "
            "WHERE (id = ANY(%s) OR group_id = ANY(%s)) AND status = 'scheduled'",
            (released, released),
        )


def validate_selection(conn: psycopg.Connection, plan: InterviewPlan) -> None:
    """Recheck sources, target versions and reservations before authenticated approval."""
    parent = store.load_session(conn, plan.parent_session_id) if plan.parent_session_id else None
    if parent is None or parent.run_id != plan.run_id or parent.status != "finished":
        raise ConflictError("the parent interview is no longer finished")
    live = {g["id"] for g in gap_store.candidates(conn, plan.run_id)}
    for topic in plan.topics:
        for seed in topic.questions:
            row = conn.execute(
                "SELECT kind,status,plan_id FROM follow_up_gaps WHERE id=%s AND run_id=%s",
                (UUID(str(seed.gap_id)), plan.run_id),
            ).fetchone()
            if row is None or row[1:] != ("scheduled", plan.id):
                raise ConflictError("selected gaps changed; prepare a new follow-up")
            if row[0] == "open_section" and seed.gap_id not in live:
                raise ConflictError("a selected section is no longer a public open gap")
        if topic.target_entry_id:
            target = conn.execute(
                "SELECT e.version,e.status FROM runbook_entries e JOIN sessions s ON "
                "s.id=e.session_id JOIN interview_plans p ON p.id=s.plan_id "
                "WHERE e.id=%s AND p.run_id=%s",
                (UUID(topic.target_entry_id), plan.run_id),
            ).fetchone()
            if target is None or target[0] != topic.base_version or target[1] == "rejected":
                raise ConflictError("follow-up target changed; prepare and review a new plan")


def validate_edit(before: InterviewPlan, after: InterviewPlan) -> None:
    """Plan review can change seeds and limits, never forge a selected source or target."""
    if before.kind != "follow_up":
        return
    if before.status != "draft" or after.status != "draft":
        raise ConflictError("the approved follow-up plan is frozen")
    if any(
        getattr(before, key) != getattr(after, key)
        for key in (
            "id",
            "run_id",
            "parent_session_id",
            "kind",
            "revision",
        )
    ):
        raise ValueError("follow-up lineage cannot be edited")
    for topic in after.topics:
        original = before.topic(topic.topic_id)
        if original is None or any(
            getattr(original, key) != getattr(topic, key)
            for key in ("target_entry_id", "base_version", "artifact_ids", "origin")
        ):
            raise ValueError("follow-up targets and scope cannot be edited")
        seeds = {q.gap_id: q for q in original.questions}
        for question in topic.questions:
            seed = seeds.get(question.gap_id)
            if seed is None or seed.section != question.section:
                raise ValueError("follow-up gap attribution cannot be edited")
        expected_scope = (
            None
            if original.sections is None
            else tuple(s for s in original.sections if any(q.section == s for q in topic.questions))
        )
        if topic.sections != expected_scope:
            raise ValueError("follow-up sections must match the selected seeds")


@dataclass
class FollowUpService:
    dsn: str = field(repr=False)
    domains: frozenset[str] = frozenset()
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)

    def _connect(self) -> psycopg.Connection:
        return psycopg.connect(self.dsn, autocommit=True)

    def _clean(self, text: str) -> RedactedText:
        return redact(text.strip(), work_email_domains=self.domains).text

    def usage(self, session_id: UUID) -> dict[str, Any]:
        with self._connect() as conn:
            return usage(conn, session_id)

    def prepare(
        self,
        run_id: UUID,
        principal: Principal,
        *,
        request_id: UUID,
        parent_session_id: UUID,
        gap_ids: Sequence[UUID | str],
        limits: FollowUpLimits | None = None,
    ) -> InterviewPlan:
        """Prepare a draft follow-up plan from selected, triaged gaps. No model is called.

        The plan ID is the client's request ID, so a retry returns the same plan. The
        selected gaps are reserved as ``scheduled`` until the plan is edited, stopped or
        finished.
        """
        limits = limits or DEFAULT_LIMITS
        ids = [str(g) for g in gap_ids]
        if not ids or len(ids) != len(set(ids)) or len(ids) > 50:
            raise ValueError("select between 1 and 50 distinct gaps")
        with acting_as(principal), self._connect() as conn, conn.transaction():
            maintenance.capture(conn, run_id)
            maintenance.lock_run(conn, run_id)
            parent = store.load_session(conn, parent_session_id)
            if parent is None or parent.run_id != run_id or parent.kind != "capture":
                raise NotFoundError("parent session not found")
            if parent.status != "finished":
                raise ConflictError("the parent interview must be finished first")
            fingerprint = hashlib.sha256(
                json.dumps(
                    {
                        "gaps": ids,
                        "parent": str(parent_session_id),
                        "limits": limits.model_dump(),
                    },
                    sort_keys=True,
                ).encode()
            ).hexdigest()
            existing = store.load_plan(conn, request_id)
            if existing is not None:
                if existing.run_id != run_id or existing.kind != "follow_up":
                    raise NotFoundError("plan not found")
                saved = conn.execute(
                    "SELECT request_fingerprint FROM interview_plans WHERE id=%s",
                    (request_id,),
                ).fetchone()
                if saved is None or saved[0] != fingerprint:
                    raise ConflictError("request ID already used for a different follow-up")
                return existing
            active = conn.execute(
                "SELECT 1 FROM interview_plans p LEFT JOIN sessions s ON s.plan_id = p.id "
                "WHERE p.run_id = %s AND p.kind = 'follow_up' AND ((p.status = 'draft' AND "
                "EXISTS (SELECT 1 FROM plan_topics t WHERE t.plan_id = p.id)) OR "
                "(p.status = 'approved' AND (s.id IS NULL OR s.status = 'active'))) LIMIT 1",
                (run_id,),
            ).fetchone()
            if active is not None:
                raise ConflictError(
                    "another active follow-up exists for this run; finish or stop it first"
                )
            gaps = gap_store.GapService(self.dsn, self.domains, self.clock)
            # One scan of the run's entries serves every selected gap.
            live = {c["id"]: c for c in gap_store.candidates(conn, run_id)}
            rows = {g: gaps.lock_gap(conn, run_id, UUID(g), live_gaps=live) for g in sorted(ids)}
            for row in rows.values():
                if row["status"] == "scheduled":
                    raise ConflictError("a selected gap is already scheduled in a follow-up")
                if row["status"] != "pending":
                    raise ValueError(f"gap is {row['status']}; reopen it in triage first")
                if row["group_id"] is not None:
                    raise ValueError("select the leading gap of a group, not a grouped one")
            followers = [
                r[0]
                for r in conn.execute(
                    "SELECT id FROM follow_up_gaps WHERE run_id = %s AND status = 'pending' "
                    "AND group_id = ANY(%s) FOR UPDATE",
                    (run_id, [UUID(g) for g in ids]),
                ).fetchall()
            ]
            parent_plan = store.load_plan(conn, parent.plan_id)
            topics: list[PlannedTopic] = []
            mapped: dict[str, list[tuple[dict[str, Any], SectionName]]] = {}
            for g in ids:
                row = rows[g]
                if row["kind"] == "open_section":
                    mapped.setdefault(str(row["entry_id"]), []).append((row, row["section"]))
                elif row["new_topic"]:
                    topics.append(self._new_topic(row))
                elif row["target_entry_id"] is not None:
                    mapped.setdefault(str(row["target_entry_id"]), []).append(
                        (row, row["target_section"])
                    )
                else:
                    raise ValueError(
                        "triage the request first: map it to an entry and section or choose "
                        "a new topic"
                    )
            for target, items in mapped.items():
                found = conn.execute(
                    "SELECT e.title, e.topic_id, e.version, e.status, e.content "
                    "FROM runbook_entries e JOIN sessions s ON s.id = e.session_id "
                    "JOIN interview_plans p ON p.id = s.plan_id WHERE e.id = %s AND p.run_id = %s",
                    (UUID(target), run_id),
                ).fetchone()
                if found is None:
                    raise NotFoundError("target entry not found")
                title, topic_id, version, status, content = found
                if status == "rejected":
                    raise ConflictError("reopen the rejected target entry in review first")
                entry = RunbookEntry.model_validate(content)
                questions = tuple(self._seed(row, section, title, entry) for row, section in items)
                wanted = {section for _, section in items}
                original = parent_plan.topic(topic_id) if parent_plan is not None else None
                topics.append(
                    PlannedTopic(
                        topic_id=topic_id,
                        label=title,
                        kind=original.kind if original is not None else "follow_up",
                        system=original.system if original is not None else None,
                        origin="follow_up",
                        score=None,
                        reason=f"follow-up on {len(items)} selected gap(s)",
                        artifact_ids=original.artifact_ids if original is not None else (),
                        questions=questions,
                        sections=tuple(s for s in SECTIONS if s in wanted),
                        target_entry_id=target,
                        base_version=version,
                    )
                )
            if len(topics) > limits.topics:
                raise ValueError(
                    f"topic limit of {limits.topics} exceeded; remove or defer selected gaps"
                )
            total = sum(len(t.questions) for t in topics)
            if total > limits.questions:
                raise ValueError(
                    f"question limit of {limits.questions} exceeded; group, remove or defer "
                    "excess questions"
                )
            plan = InterviewPlan(
                id=request_id,
                run_id=run_id,
                kind="follow_up",
                parent_session_id=parent_session_id,
                limits=limits,
                topics=tuple(topics),
            )
            store.save_plan(conn, plan)
            conn.execute(
                "UPDATE interview_plans SET request_fingerprint=%s WHERE id=%s",
                (fingerprint, request_id),
            )
            reserved = [UUID(g) for g in ids] + followers
            conn.execute(
                "UPDATE follow_up_gaps SET status = 'scheduled', plan_id = %s, "
                "revision = revision + 1 WHERE id = ANY(%s)",
                (request_id, reserved),
            )
            append_event(
                conn,
                "review",
                "completed",
                AuditDetails(
                    operation="follow_up_prepared", target_id=request_id, count=len(reserved)
                ),
                run_id=run_id,
            )
            return plan

    def _seed(
        self, row: dict[str, Any], section: SectionName, title: str, entry: RunbookEntry
    ) -> Question:
        text = row.get("seed_question") or row.get("question")
        if text is None:
            section_name = next(s for s in SECTIONS if s == section)
            open_question = entry.section(section_name).open_question
            if open_question is None:
                raise ValueError("the selected section is no longer open")
            text = (
                f'Earlier, the {section} for "{title}" was left open: {open_question} '
                "Could you cover it now?"
            )
        return Question(
            text=self._clean(text),
            origin="follow_up",
            gap_id=str(row["id"]),
            section=section,
        )

    def _new_topic(self, row: dict[str, Any]) -> PlannedTopic:
        text = row.get("seed_question") or row.get("question") or ""
        return PlannedTopic(
            topic_id=f"follow-up:{row['id']}",
            label=str(row.get("topic_label") or "Follow-up topic"),
            kind="expert",
            system=None,
            origin="follow_up",
            score=None,
            reason="new topic chosen in follow-up triage",
            artifact_ids=(),
            questions=(
                Question(text=self._clean(text), origin="follow_up", gap_id=str(row["id"])),
            ),
        )

    def discard(
        self, plan_id: UUID, principal: Principal, *, expected_revision: int
    ) -> InterviewPlan:
        """Abandon a draft follow-up: empty its plan and return every selected gap to triage.

        Only an owner or administrator may discard; an approved follow-up is stopped instead.
        Discarding an already empty draft is a harmless no-op.
        """
        with acting_as(principal), self._connect() as conn, conn.transaction():
            row = conn.execute(
                "SELECT run_id FROM interview_plans WHERE id = %s AND kind = 'follow_up'",
                (plan_id,),
            ).fetchone()
            if row is None:
                raise NotFoundError("plan not found")
            run_id = UUID(str(row[0]))
            maintenance.active(conn, principal)
            maintenance.lock_run(conn, run_id)
            from eki.access import require_manager

            require_manager(conn, run_id, principal)
            conn.execute("SELECT 1 FROM interview_plans WHERE id = %s FOR UPDATE", (plan_id,))
            plan = store.load_plan(conn, plan_id)
            if plan is None:
                raise NotFoundError("plan not found")
            if plan.status != "draft":
                raise ConflictError("an approved follow-up cannot be discarded; stop it instead")
            if not plan.topics:
                return plan
            if plan.revision != expected_revision:
                raise ConflictError("plan changed; refresh before discarding")
            emptied = plan.model_copy(update={"topics": ()})
            store.save_plan(conn, emptied)
            release(conn, plan, emptied)
            append_event(
                conn,
                "review",
                "completed",
                AuditDetails(operation="follow_up_discarded", target_id=plan_id),
                run_id=run_id,
            )
            saved = store.load_plan(conn, plan_id)
            assert saved is not None  # noqa: S101 - just saved
            return saved

    def stop(self, session_id: UUID, principal: Principal) -> dict[str, Any]:
        """The respondent stops further questions; accepted work is kept and settled first."""
        with acting_as(principal), self._connect() as conn:
            session = store.load_session(conn, session_id)
            if session is None:
                raise NotFoundError("session not found")
            if session.kind != "follow_up":
                raise ConflictError("only follow-up sessions can be stopped")
            maintenance.capture(conn, session.run_id)
            respondent(conn, session.run_id, principal)
            if session.status == "finished":
                return usage(conn, session_id)
            if not store.try_lock(conn, session.plan_id):
                raise ConflictError("another message for this session is being handled")
            try:
                if delivery.pending(conn, session_id):
                    raise ConflictError("recover the saved message before stopping")
                plan = store.load_plan(conn, session.plan_id)
                with conn.transaction():
                    if session.current_topic_id is not None and plan is not None:
                        topic = plan.topic(session.current_topic_id)
                        in_scope = topic.sections if topic and topic.sections else SECTIONS
                        checklist = store.checklist(conn, session_id, session.current_topic_id)
                        for section in in_scope:
                            if checklist[section] == "missing":
                                store.mark_open(
                                    conn,
                                    session_id,
                                    session.current_topic_id,
                                    section,
                                    STOPPED,
                                    turn=None,
                                )
                    return finish(conn, session_id, "expert_stopped", now=self.clock())
            finally:
                store.unlock(conn, session.plan_id)
