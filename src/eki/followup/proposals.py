"""F22 9c: private follow-up proposals for existing entries.

A proposal is one row per (finished follow-up session, mapped topic). It holds the new
public facts of the selected sections, each citing its own follow-up turn, and it is
also the explicit entry <- follow-up session lineage that citation checks accept.
Nothing here changes an entry; ``RunbookService.apply_proposal`` does, on request.

Lock order (global, see ``eki.maintenance``): run, session, entries (ascending id),
proposals (ascending id), gaps (ascending id), flags, then the audit advisory lock last.
This module never imports ``eki.runbook.service`` or ``eki.followup.sessions``.
"""

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, Literal
from uuid import UUID, uuid5

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field

from eki.audit import AuditDetails, append_event
from eki.interview import store as interviews
from eki.interview.models import Note, OpenItem, PlannedTopic
from eki.runbook import review
from eki.runbook.faithfulness import Unsupported
from eki.runbook.models import SECTION_NAMES, Claim, RunbookEntry, Section, SectionName
from eki.runbook.store import StoredEntry
from eki.runbook.synthesis import CONFIDENTIAL, public_claims

ApplyMode = Literal["merge", "replace"]


class SectionPatch(BaseModel):
    """New public facts for one section, the seeds asked about it and whether it closes."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    claims: tuple[Claim, ...] = Field(min_length=1)
    gap_ids: tuple[str, ...]
    clears_open: bool


def proposal_id(session_id: UUID, topic_id: str) -> UUID:
    return uuid5(session_id, "proposal:" + topic_id)


def build_patch(
    session_id: UUID,
    topic: PlannedTopic,
    notes: Sequence[Note],
    open_items: Sequence[OpenItem],
    outcomes: Mapping[str, str],
    base: RunbookEntry,
) -> dict[SectionName, SectionPatch]:
    """Only selected sections with at least one active public note get a patch (P2).

    A section clears its open question only when every seed asked about it was answered,
    nothing about it is still open in the follow-up and it is not the confidential marker.
    """
    still_open = {o.section for o in open_items if o.topic_id == topic.topic_id}
    patch: dict[SectionName, SectionPatch] = {}
    for section in topic.sections or ():
        claims = public_claims(session_id, topic.topic_id, section, notes)
        if not claims:
            continue
        gap_ids = tuple(q.gap_id for q in topic.questions if q.section == section and q.gap_id)
        clears = (
            bool(gap_ids)
            and all(outcomes.get(g) == "answered" for g in gap_ids)
            and section not in still_open
            and base.section(section).open_question != CONFIDENTIAL
        )
        patch[section] = SectionPatch(claims=claims, gap_ids=gap_ids, clears_open=clears)
    return patch


def parse_patch(raw: Mapping[str, Any]) -> dict[SectionName, SectionPatch]:
    return {name: SectionPatch.model_validate(raw[name]) for name in SECTION_NAMES if name in raw}


def dump_patch(patch: Mapping[SectionName, SectionPatch]) -> dict[str, Any]:
    return {name: section.model_dump(mode="json") for name, section in patch.items()}


def selected(
    entry: RunbookEntry,
    patch: Mapping[SectionName, SectionPatch],
    sections: Mapping[SectionName, ApplyMode],
) -> RunbookEntry:
    """The entry with the chosen sections merged or replaced; everything else unchanged.

    The result keeps the entry id and becomes a draft with its approval cleared.
    """
    changed: dict[SectionName, Section] = {}
    for name, mode in sections.items():
        base, add = entry.section(name), patch[name]
        changed[name] = Section(
            claims=(base.claims if mode == "merge" else ()) + add.claims,
            open_question=None if add.clears_open else base.open_question,
        )
    return review.edit(entry, changed)


def fingerprint(
    sections: Mapping[SectionName, ApplyMode], expected_revision: int, expected_version: int
) -> str:
    canonical = json.dumps(
        {
            "sections": dict(sorted(sections.items())),
            "revision": expected_revision,
            "version": expected_version,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def ensure(
    conn: psycopg.Connection, session_id: UUID, outcomes: Mapping[str, str], *, now: datetime
) -> tuple[UUID, ...]:
    """Create the proposals of a finished follow-up once; return only the inserted ids.

    The caller holds the follow-up session row lock and audits the result last
    (``audit_proposed``). Targets outside the run, or no longer of the planned topic,
    are skipped, and so are topics without public facts. Replays insert nothing.
    """
    session = interviews.load_session(conn, session_id)
    if session is None or session.status != "finished":
        return ()
    plan = interviews.load_plan(conn, session.plan_id)
    if plan is None or plan.kind != "follow_up":
        return ()
    notes = interviews.load_notes(conn, session_id)
    opened = interviews.load_open_items(conn, session_id)
    created: list[UUID] = []
    for topic in plan.topics:
        if topic.target_entry_id is None or topic.base_version is None:
            continue
        row = conn.execute(
            "SELECT e.content FROM runbook_entries e JOIN sessions s ON s.id=e.session_id "
            "JOIN interview_plans p ON p.id=s.plan_id "
            "WHERE e.id=%s AND p.run_id=%s AND e.topic_id=%s",
            (UUID(topic.target_entry_id), session.run_id, topic.topic_id),
        ).fetchone()
        if row is None:
            continue
        patch = build_patch(
            session_id, topic, notes, opened, outcomes, RunbookEntry.model_validate(row[0])
        )
        if not patch:
            continue
        pid = proposal_id(session_id, topic.topic_id)
        inserted = conn.execute(
            "INSERT INTO follow_up_proposals (id, session_id, topic_id, entry_id, base_version, "
            "patch, gap_ids, created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (session_id, topic_id) DO NOTHING RETURNING id",
            (
                pid,
                session_id,
                topic.topic_id,
                UUID(topic.target_entry_id),
                topic.base_version,
                Jsonb(dump_patch(patch)),
                [UUID(q.gap_id) for q in topic.questions if q.gap_id],
                now,
            ),
        ).fetchone()
        if inserted is not None:
            created.append(pid)
    return tuple(created)


def audit_proposed(conn: psycopg.Connection, run_id: UUID, ids: Sequence[UUID]) -> None:
    """One metadata-only event per created proposal: id, base version, section count."""
    for pid in ids:
        row = load(conn, pid)
        if row is None:
            continue
        append_event(
            conn,
            "review",
            "completed",
            AuditDetails(
                operation="follow_up_proposed",
                target_id=pid,
                version=row["base_version"],
                count=len(row["patch"]),
            ),
            run_id=run_id,
        )


def load(conn: psycopg.Connection, pid: UUID, *, lock: bool = False) -> dict[str, Any] | None:
    with conn.cursor(row_factory=dict_row) as cursor:
        query = (
            "SELECT * FROM follow_up_proposals WHERE id=%s FOR UPDATE"
            if lock
            else "SELECT * FROM follow_up_proposals WHERE id=%s"
        )
        row = cursor.execute(query, (pid,)).fetchone()
    return None if row is None else dict(row)


def for_session(conn: psycopg.Connection, session_id: UUID) -> list[dict[str, Any]]:
    with conn.cursor(row_factory=dict_row) as cursor:
        rows = cursor.execute(
            "SELECT * FROM follow_up_proposals WHERE session_id=%s ORDER BY id", (session_id,)
        ).fetchall()
    return [dict(r) for r in rows]


def applied_to(conn: psycopg.Connection, entry_id: str) -> bool:
    """Whether reviewed follow-up facts were applied; re-synthesis must then keep the entry."""
    return (
        conn.execute(
            "SELECT 1 FROM follow_up_proposals WHERE entry_id=%s AND status='applied' LIMIT 1",
            (UUID(entry_id),),
        ).fetchone()
        is not None
    )


def linked_sessions(conn: psycopg.Connection, entry_id: str, topic_id: str) -> tuple[UUID, ...]:
    """Follow-up sessions explicitly linked to an entry by a proposal of its own topic (P5)."""
    rows = conn.execute(
        "SELECT DISTINCT session_id FROM follow_up_proposals WHERE entry_id=%s AND topic_id=%s "
        "ORDER BY session_id",
        (UUID(entry_id), topic_id),
    ).fetchall()
    return tuple(UUID(str(r[0])) for r in rows)


def linked_entries(conn: psycopg.Connection, session_id: UUID) -> tuple[str, ...]:
    """Entries a follow-up session proposed facts for (P6 correction propagation)."""
    rows = conn.execute(
        "SELECT DISTINCT entry_id FROM follow_up_proposals WHERE session_id=%s ORDER BY entry_id",
        (session_id,),
    ).fetchall()
    return tuple(str(r[0]) for r in rows)


def view(
    row: Mapping[str, Any],
    current: StoredEntry,
    info: Mapping[str, Any],
    unsupported: Sequence[Unsupported],
) -> dict[str, Any]:
    """The owner's comparison: current base, added facts and both apply results per section.

    Only public note claims and the fixed confidential marker can appear here.
    """
    patch = parse_patch(row["patch"])
    entry = current.entry
    pending = row["status"] == "pending"
    sections = []
    for name, add in patch.items():
        base = entry.section(name)
        open_question = None if add.clears_open else base.open_question
        sections.append(
            {
                "section": name,
                "gap_ids": list(add.gap_ids),
                "clears_open": add.clears_open,
                "replaces_existing_claims": bool(base.claims),
                "base": base.model_dump(mode="json"),
                "added": [c.model_dump(mode="json") for c in add.claims],
                "merged": Section(
                    claims=base.claims + add.claims, open_question=open_question
                ).model_dump(mode="json"),
                "replaced": Section(claims=add.claims, open_question=open_question).model_dump(
                    mode="json"
                ),
            }
        )
    return {
        "id": str(row["id"]),
        "session_id": str(row["session_id"]),
        "entry_id": str(row["entry_id"]),
        "topic_id": row["topic_id"],
        "title": entry.title,
        "status": row["status"],
        "revision": row["revision"],
        "base_version": row["base_version"],
        "entry_version": current.version,
        "entry_status": entry.status.value,
        "maintenance_revision": info.get("maintenance_revision"),
        "stale": pending
        and (current.version != row["base_version"] or entry.status.value == "rejected"),
        "edited": current.edited,
        "can_apply": pending and bool(info.get("can_review")),
        "gap_ids": [str(g) for g in row["gap_ids"]],
        "sections": sections,
        "unchanged_sections": [name for name in SECTION_NAMES if name not in patch],
        "unsupported": [
            {"section": u.section, "claim_index": u.index, "terms": list(u.terms)}
            for u in unsupported
        ],
        "applied_version": row["applied_version"],
        "applied_sections": row["applied_sections"],
        "applied_at": None if row["applied_at"] is None else row["applied_at"].isoformat(),
    }
