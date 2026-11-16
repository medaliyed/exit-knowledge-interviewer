"""F22 gap intake: live public open sections plus explicitly saved reader questions.

Lock order: run, session, entry, gap rows (ascending id), audit last. Intake and
triage need only the run and gaps. Readers only see their own safe request receipts.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID, uuid4, uuid5

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from eki import access, maintenance, throttle
from eki.audit import AuditDetails, append_event
from eki.auth import Principal, acting_as
from eki.errors import ConflictError, NotFoundError
from eki.ingest.redaction import redact
from eki.interview import store as interviews
from eki.runbook import store as entry_store
from eki.runbook.models import SECTION_NAMES, RunbookEntry

GapStatus = Literal["pending", "scheduled", "awaiting_review", "resolved", "deferred", "dismissed"]
Action = Literal["map", "new_topic", "defer", "dismiss", "reopen", "group"]
OPEN = ("pending", "scheduled", "awaiting_review")
RECEIPT_FIELDS = (
    "id",
    "run_id",
    "kind",
    "request_id",
    "question",
    "entry_id",
    "section",
    "created_at",
    "status",
    "resolved_at",
    "revision",
)


def open_section_gap_id(entry_id: UUID | str, section: str) -> UUID:
    """The stable identity of an open-section candidate, before any row exists for it."""
    return uuid5(UUID(str(entry_id)), section)


def receipt(gap: dict[str, Any]) -> dict[str, Any]:
    return {key: gap[key] for key in RECEIPT_FIELDS}


def _public(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    for key in ("id", "run_id", "entry_id", "target_entry_id", "request_id", "group_id"):
        if out.get(key) is not None:
            out[key] = str(out[key])
    if out.get("resolved_entry_id") is not None:
        out["resolved_entry_id"] = str(out["resolved_entry_id"])
    for key in ("created_at", "resolved_at"):
        if isinstance(out.get(key), datetime):
            out[key] = out[key].isoformat()
    return out


def candidates(conn: psycopg.Connection, run_id: UUID) -> list[dict[str, Any]]:
    """Current public open sections of the run's draft and approved entries.

    A section whose source note is confidential is not a public gap, whatever the
    entry text says; rejected entries are excluded until they are reopened in review.
    """
    rows = conn.execute(
        "SELECT e.id,e.title,e.status,e.version,e.topic_id,e.session_id,e.content "
        "FROM runbook_entries e JOIN sessions s ON s.id=e.session_id "
        "JOIN interview_plans p ON p.id=s.plan_id WHERE p.run_id=%s AND "
        "e.status IN ('draft','approved') ORDER BY e.id",
        (run_id,),
    ).fetchall()
    notes_by_session: dict[UUID, tuple[Any, ...]] = {}
    result: list[dict[str, Any]] = []
    for entry_id, title, status, version, topic_id, session_id, content in rows:
        entry = RunbookEntry.model_validate(content)
        notes = notes_by_session.get(session_id)
        if notes is None:
            notes = interviews.load_notes(conn, session_id)
            notes_by_session[session_id] = notes
        confidential = {
            n.section
            for n in notes
            if n.topic_id == topic_id and n.confidential and not n.superseded
        }
        for section in SECTION_NAMES:
            question = entry.section(section).open_question
            if question is None or section in confidential:
                continue
            result.append(
                {
                    "id": str(open_section_gap_id(entry_id, section)),
                    "run_id": str(run_id),
                    "kind": "open_section",
                    "entry_id": str(entry_id),
                    "section": section,
                    "entry_title": title,
                    "entry_status": status,
                    "entry_version": version,
                    "open_question": str(question),
                }
            )
    return result


@dataclass
class GapService:
    dsn: str = field(repr=False)
    domains: frozenset[str] = frozenset()
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)

    def _connect(self) -> psycopg.Connection:
        return psycopg.connect(self.dsn, autocommit=True)

    def _clean(self, text: str) -> str:
        return str(redact(text.strip(), work_email_domains=self.domains).text)

    def _visible(self, conn: psycopg.Connection, run_id: UUID, principal: Principal) -> None:
        access.visible_run(conn, run_id, principal)

    # --- reading ---

    def queue(
        self,
        run_id: UUID,
        principal: Principal,
        *,
        filter: str = "open",
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        if (
            filter not in ("open", "all", "deferred", "dismissed", "requests", "open_sections")
            or not 1 <= limit <= 100
            or offset < 0
        ):
            raise ValueError("invalid gap filter or page")
        with self._connect() as conn:
            maintenance.active(conn, principal)
            access.require_manager(conn, run_id, principal)
            with conn.cursor(row_factory=dict_row) as cursor:
                rows = cursor.execute(
                    "SELECT * FROM follow_up_gaps WHERE run_id=%s ORDER BY created_at,id",
                    (run_id,),
                ).fetchall()
            states = {str(r["id"]): _public(r) for r in rows if r["kind"] == "open_section"}
            merged: list[dict[str, Any]] = []
            live_ids: set[str] = set()
            for live in candidates(conn, run_id):
                live_ids.add(live["id"])
                state = states.get(live["id"])
                status = state["status"] if state else "pending"
                # A resolved section that a later edit reopened is a new pending gap.
                reopened = status == "resolved"
                merged.append(
                    {
                        **_blank_state(),
                        **(state or {}),
                        **live,
                        "status": "pending" if reopened else status,
                        "live": True,
                        **({"reopened": True} if reopened else {}),
                    }
                )
            # Answered, scheduled and resolved work stays visible after its section
            # fills; untouched pending, deferred and dismissed history does not (A1).
            settled = [
                state
                for gap_id, state in states.items()
                if gap_id not in live_ids
                and state["status"] in ("scheduled", "awaiting_review", "resolved")
            ]
            entries = {
                str(r[0]): r[1:]
                for r in conn.execute(
                    "SELECT id,title,status,version FROM runbook_entries WHERE id = ANY(%s)",
                    ([UUID(s["entry_id"]) for s in settled],),
                ).fetchall()
            }
            for state in settled:
                title, entry_status, version = entries.get(state["entry_id"], (None, None, None))
                merged.append(
                    {
                        **_blank_state(),
                        **state,
                        "entry_title": title,
                        "entry_status": entry_status,
                        "entry_version": version,
                        "open_question": None,
                        "live": False,
                    }
                )
            for r in rows:
                if r["kind"] == "request":
                    merged.append(_public(r))
            attempts: dict[str, list[dict[str, Any]]] = {}
            for gap_id, session_id, outcome, question_turn, recorded_at in conn.execute(
                "SELECT a.gap_id,a.session_id,a.outcome,a.question_turn,a.recorded_at "
                "FROM follow_up_attempts a JOIN follow_up_gaps g ON g.id=a.gap_id "
                "WHERE g.run_id=%s ORDER BY a.id",
                (run_id,),
            ).fetchall():
                attempts.setdefault(str(gap_id), []).append(
                    {
                        "session_id": str(session_id),
                        "outcome": outcome,
                        "question_turn": question_turn,
                        "recorded_at": recorded_at.isoformat(),
                    }
                )
            for gap in merged:
                gap["attempts"] = attempts.get(str(gap.get("group_id") or gap["id"]), [])
        selected = [g for g in merged if _matches(filter, g)]
        selected.sort(
            key=lambda g: (
                -int(g.get("priority") or 0),
                g["kind"] != "open_section",
                str(g.get("created_at") or ""),
                g["id"],
            )
        )
        page = selected[offset : offset + limit]
        return {
            "gaps": page,
            "next_offset": offset + limit if len(selected) > offset + limit else None,
        }

    def receipt(self, gap_id: UUID | str, principal: Principal) -> dict[str, Any]:
        with self._connect() as conn:
            maintenance.active(conn, principal)
            with conn.cursor(row_factory=dict_row) as cursor:
                row = cursor.execute(
                    "SELECT * FROM follow_up_gaps WHERE id=%s", (gap_id,)
                ).fetchone()
            if row is None:
                raise NotFoundError("request not found")
            run_id = UUID(str(row["run_id"]))
            self._visible(conn, run_id, principal)
            try:
                access.require_manager(conn, run_id, principal)
            except NotFoundError:
                if row["kind"] != "request" or row["requester_subject"] != principal.subject:
                    raise
                return receipt(_public(row))
            return _public(row)

    # --- explicit saves ---

    def save_request(
        self,
        run_id: UUID,
        principal: Principal,
        *,
        request_id: UUID,
        question: str,
        entry_id: UUID | None = None,
        section: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        if not question.strip() or len(question) > 2000:
            raise ValueError("a question needs 1 to 2000 characters")
        if section is not None and (section not in SECTION_NAMES or entry_id is None):
            raise ValueError("a section reference needs a valid section and an entry")
        clean = self._clean(question)
        now = self.clock()
        with acting_as(principal), self._connect() as conn, conn.transaction():
            maintenance.lock_run(conn, run_id)
            maintenance.active(conn, principal)
            self._visible(conn, run_id, principal)
            if entry_id is not None:
                if access.resource_run(conn, "entries", entry_id) != run_id:
                    raise NotFoundError("entry not found")
                access.approved_entry(conn, entry_id, principal)
            with conn.cursor(row_factory=dict_row) as cursor:
                previous = cursor.execute(
                    "SELECT * FROM follow_up_gaps WHERE run_id=%s AND requester_subject=%s "
                    "AND request_id=%s",
                    (run_id, principal.subject, request_id),
                ).fetchone()
                if previous is not None:
                    same = (
                        previous["question"],
                        previous["entry_id"],
                        previous["section"],
                    ) == (clean, entry_id, section)
                    if not same:
                        raise ConflictError("request ID already used for a different question")
                    return receipt(_public(previous)), False
                repeated = cursor.execute(
                    "SELECT * FROM follow_up_gaps WHERE run_id=%s AND requester_subject=%s "
                    "AND question=%s AND status NOT IN ('resolved','dismissed') "
                    "ORDER BY created_at LIMIT 1",
                    (run_id, principal.subject, clean),
                ).fetchone()
                if repeated is not None:
                    return receipt(_public(repeated)), False
                throttle.take(conn, "follow_up_throttle", principal.subject, now)
                gap_id = uuid4()
                row = cursor.execute(
                    "INSERT INTO follow_up_gaps(id,run_id,kind,entry_id,section,requester_subject,"
                    "request_id,question,created_at) VALUES(%s,%s,'request',%s,%s,%s,%s,%s,%s) "
                    "RETURNING *",
                    (gap_id, run_id, entry_id, section, principal.subject, request_id, clean, now),
                ).fetchone()
            append_event(
                conn,
                "review",
                "completed",
                AuditDetails(operation="follow_up_requested", target_id=gap_id),
                run_id=run_id,
            )
            if row is None:
                raise RuntimeError("request not saved")
            return receipt(_public(row)), True

    # --- owner triage ---

    def resolve(
        self,
        run_id: UUID,
        gap_id: UUID | str,
        principal: Principal,
        *,
        expected_revision: int,
        entry_id: UUID,
        expected_version: int,
        confirms_answer: bool,
    ) -> dict[str, Any]:
        """Explicit reviewer confirmation that an approved version answers a saved request.

        No model judges the answer and approval of an entry never closes a reader's
        request by itself. The target, authority and optimistic versions are checked
        again under the same locks used for entry review, including in maintenance.
        """
        if confirms_answer is not True or expected_revision < 0 or expected_version < 1:
            raise ValueError("confirm the approved version answers this request")
        with acting_as(principal), self._connect() as conn, conn.transaction():
            owner, _, _, _ = maintenance.lock_run(conn, run_id)
            if principal.subject != owner:
                raise NotFoundError("request not found")
            maintenance.reviewer(conn, run_id, principal.name)
            access.require_manager(conn, run_id, principal)
            target = conn.execute(
                "SELECT e.session_id FROM runbook_entries e JOIN sessions s ON s.id=e.session_id "
                "JOIN interview_plans p ON p.id=s.plan_id WHERE e.id=%s AND p.run_id=%s",
                (entry_id, run_id),
            ).fetchone()
            if target is None:
                raise NotFoundError("entry not found")
            conn.execute("SELECT 1 FROM sessions WHERE id=%s FOR UPDATE", (target[0],))
            stored = entry_store.load_entry(conn, str(entry_id), lock=True)
            if stored is None:
                raise NotFoundError("entry not found")
            if stored.version != expected_version or stored.entry.status != "approved":
                raise ConflictError("review the current approved entry version before resolving")
            row = self.lock_gap(conn, run_id, gap_id, require_live=False)
            if row["kind"] != "request":
                raise ValueError("open sections resolve through entry approval")
            if row["status"] == "resolved":
                if (
                    row["resolved_entry_id"] == entry_id
                    and row["resolved_version"] == expected_version
                    and row["revision"] in (expected_revision, expected_revision + 1)
                ):
                    return _public(row)
                raise ConflictError("request already resolved; refresh its receipt")
            if row["revision"] != expected_revision:
                raise ConflictError("gap changed; refresh before resolving")
            if row["status"] not in ("pending", "deferred", "awaiting_review"):
                raise ConflictError("settle the interview or reopen this request before resolving")
            saved = _resolve_row(
                conn, row["id"], entry_id, expected_version, principal.subject, self.clock()
            )
            audit_resolution(conn, run_id, saved)
            return _public(saved)

    def triage(
        self,
        run_id: UUID,
        gap_id: UUID | str,
        principal: Principal,
        *,
        action: Action,
        expected_revision: int,
        target_entry_id: UUID | None = None,
        target_section: str | None = None,
        seed_question: str | None = None,
        priority: int | None = None,
        reason: str | None = None,
        topic_label: str | None = None,
        group_with: UUID | None = None,
    ) -> dict[str, Any]:
        if action not in ("map", "new_topic", "defer", "dismiss", "reopen", "group"):
            raise ValueError("unknown triage action")
        if action in ("defer", "dismiss") and (
            not (reason or "").strip() or len(reason or "") > 2000
        ):
            raise ValueError("deferral and dismissal need a reason")
        if seed_question is not None and (not seed_question.strip() or len(seed_question) > 2000):
            raise ValueError("a seed question needs 1 to 2000 characters")
        if priority is not None and not 0 <= priority <= 10:
            raise ValueError("priority must be between 0 and 10")
        if action == "new_topic" and (
            not (topic_label or "").strip() or len(topic_label or "") > 200
        ):
            raise ValueError("a new topic needs a label")
        with acting_as(principal), self._connect() as conn, conn.transaction():
            maintenance.lock_run(conn, run_id)
            maintenance.active(conn, principal)
            access.require_manager(conn, run_id, principal)
            row = self.lock_gap(conn, run_id, gap_id)
            if row["revision"] != expected_revision:
                raise ConflictError("gap changed; refresh before triaging")
            status: str = row["status"]
            update: dict[str, Any] = {}
            if action in ("map", "new_topic"):
                if status not in ("pending", "deferred"):
                    raise ConflictError("only pending or deferred gaps can be mapped")
                if action == "map":
                    if target_entry_id is None or target_section not in SECTION_NAMES:
                        raise ValueError("mapping needs a target entry and section")
                    target = conn.execute(
                        "SELECT e.status FROM runbook_entries e JOIN sessions s ON "
                        "s.id=e.session_id JOIN interview_plans p ON p.id=s.plan_id "
                        "WHERE e.id=%s AND p.run_id=%s",
                        (target_entry_id, run_id),
                    ).fetchone()
                    if target is None:
                        raise NotFoundError("target entry not found")
                    if target[0] == "rejected":
                        raise ConflictError("reopen the rejected entry in review before mapping")
                    update.update(
                        target_entry_id=target_entry_id,
                        target_section=target_section,
                        new_topic=False,
                        topic_label=None,
                    )
                else:
                    update.update(
                        target_entry_id=None,
                        target_section=None,
                        new_topic=True,
                        topic_label=self._clean(topic_label or ""),
                    )
                if seed_question is not None:
                    update["seed_question"] = self._clean(seed_question)
                if priority is not None:
                    update["priority"] = priority
                update["status"] = "pending"
            elif action in ("defer", "dismiss"):
                if status not in ("pending", "deferred"):
                    raise ConflictError("scheduled or finished gaps cannot be deferred")
                update.update(status="deferred" if action == "defer" else "dismissed")
                update["reason"] = self._clean(reason or "")
            elif action == "reopen":
                if status not in ("deferred", "dismissed", "resolved"):
                    raise ConflictError("only deferred, dismissed or resolved gaps can be reopened")
                update.update(
                    status="pending",
                    reason=None if reason is None else self._clean(reason),
                    resolved_entry_id=None,
                    resolved_version=None,
                    resolved_at=None,
                    resolved_by_subject=None,
                    plan_id=None,
                    group_id=None,
                )
            else:
                if group_with is None or str(group_with) == str(row["id"]):
                    raise ValueError("grouping needs another gap of this run")
                leader = self.lock_gap(conn, run_id, group_with)
                if status != "pending" or leader["status"] != "pending":
                    raise ConflictError("only pending gaps can be grouped")
                if leader["group_id"] is not None:
                    raise ConflictError("group under the leading gap, not a grouped one")
                update["group_id"] = group_with
            update["revision"] = row["revision"] + 1
            with conn.cursor(row_factory=dict_row) as cursor:
                saved = cursor.execute(
                    sql.SQL("UPDATE follow_up_gaps SET {} WHERE id=%s RETURNING *").format(
                        sql.SQL(",").join(
                            sql.SQL("{}=%s").format(sql.Identifier(k)) for k in update
                        )
                    ),
                    (*update.values(), row["id"]),
                ).fetchone()
            append_event(
                conn,
                "review",
                "completed",
                AuditDetails(
                    operation="gap_triaged",
                    target_id=UUID(str(row["id"])),
                    version=update["revision"],
                ),
                run_id=run_id,
            )
            if saved is None:
                raise RuntimeError("gap not saved")
            live = next((c for c in candidates(conn, run_id) if c["id"] == str(saved["id"])), {})
            return {**_blank_state(), **_public(saved), **live, "status": saved["status"]}

    def lock_gap(
        self,
        conn: psycopg.Connection,
        run_id: UUID,
        gap_id: UUID | str,
        *,
        require_live: bool = True,
    ) -> dict[str, Any]:
        """Lock a gap row, creating the state row of a live open-section candidate first.

        A resolved open section that a later edit reopened is reset to pending here, keeping
        its revision so the queue's expected revision still matches; the caller's change
        bumps it and audits.
        """
        with conn.cursor(row_factory=dict_row) as cursor:
            row = cursor.execute(
                "SELECT * FROM follow_up_gaps WHERE id=%s AND run_id=%s FOR UPDATE",
                (gap_id, run_id),
            ).fetchone()
            live = next((c for c in candidates(conn, run_id) if c["id"] == str(gap_id)), None)
            if row is not None:
                if row["kind"] == "open_section" and live is None:
                    if require_live:
                        raise ConflictError("the selected section is no longer a public open gap")
                    return dict(row)
                if row["kind"] == "open_section" and row["status"] == "resolved":
                    row = cursor.execute(
                        "UPDATE follow_up_gaps SET status='pending',resolved_entry_id=NULL,"
                        "resolved_version=NULL,resolved_at=NULL,resolved_by_subject=NULL,"
                        "plan_id=NULL WHERE id=%s RETURNING *",
                        (row["id"],),
                    ).fetchone()
                    assert row is not None  # noqa: S101 - locked above
                return dict(row)
            if live is None:
                raise NotFoundError("gap not found")
            cursor.execute(
                "INSERT INTO follow_up_gaps(id,run_id,kind,entry_id,section,created_at) "
                "VALUES(%s,%s,'open_section',%s,%s,%s) ON CONFLICT (id) DO NOTHING",
                (gap_id, run_id, UUID(live["entry_id"]), live["section"], self.clock()),
            )
            row = cursor.execute(
                "SELECT * FROM follow_up_gaps WHERE id=%s AND run_id=%s FOR UPDATE",
                (gap_id, run_id),
            ).fetchone()
            if row is None:
                raise NotFoundError("gap not found")
            return dict(row)


def _resolve_row(
    conn: psycopg.Connection,
    gap_id: UUID,
    entry_id: UUID,
    version: int,
    subject: str,
    at: datetime,
) -> dict[str, Any]:
    with conn.cursor(row_factory=dict_row) as cursor:
        saved = cursor.execute(
            "UPDATE follow_up_gaps SET status='resolved',resolved_entry_id=%s,"
            "resolved_version=%s,resolved_at=%s,resolved_by_subject=%s,"
            "revision=revision+1,plan_id=NULL WHERE id=%s RETURNING *",
            (entry_id, version, at, subject, gap_id),
        ).fetchone()
    if saved is None:
        raise NotFoundError("gap not found")
    return dict(saved)


def audit_resolution(conn: psycopg.Connection, run_id: UUID, saved: dict[str, Any]) -> None:
    append_event(
        conn,
        "review",
        "completed",
        AuditDetails(operation="gap_resolved", target_id=saved["id"], version=saved["revision"]),
        run_id=run_id,
    )


def resolve_approved_sections(
    conn: psycopg.Connection,
    run_id: UUID,
    stored: entry_store.StoredEntry,
    subject: str,
    at: datetime,
) -> None:
    """Close tracked public open sections only inside an explicit approval transaction.

    Source validation has already proved every claim public and cited. Active plan
    reservations stay scheduled until the interview settles; reader requests always
    require their own explicit confirmation. Acquire all gap locks before auditing.
    """
    filled = {
        name
        for name in SECTION_NAMES
        if stored.entry.section(name).claims and stored.entry.section(name).open_question is None
    }
    if stored.entry.status != "approved" or not filled:
        return
    with conn.cursor(row_factory=dict_row) as cursor:
        rows = cursor.execute(
            "SELECT * FROM follow_up_gaps WHERE run_id=%s AND kind='open_section' "
            "AND entry_id=%s AND status NOT IN ('resolved','scheduled') ORDER BY id FOR UPDATE",
            (run_id, UUID(stored.entry.id)),
        ).fetchall()
    saved = [
        _resolve_row(conn, row["id"], UUID(stored.entry.id), stored.version, subject, at)
        for row in rows
        if row["section"] in filled
    ]
    for resolved in saved:
        audit_resolution(conn, run_id, resolved)


def _blank_state() -> dict[str, Any]:
    return {
        "status": "pending",
        "target_entry_id": None,
        "target_section": None,
        "new_topic": False,
        "topic_label": None,
        "seed_question": None,
        "priority": 0,
        "reason": None,
        "group_id": None,
        "revision": 0,
        "created_at": None,
        "resolved_entry_id": None,
        "resolved_version": None,
        "resolved_at": None,
        "resolved_by_subject": None,
        "requester_subject": None,
        "request_id": None,
        "question": None,
    }


def _matches(filter: str, gap: dict[str, Any]) -> bool:
    if filter == "all":
        return True
    if filter == "requests":
        return bool(gap["kind"] == "request" and gap["status"] in OPEN)
    if filter == "open_sections":
        return bool(gap["kind"] == "open_section" and gap["status"] in OPEN)
    if filter in ("deferred", "dismissed"):
        return bool(gap["status"] == filter)
    return bool(gap["status"] in OPEN)
