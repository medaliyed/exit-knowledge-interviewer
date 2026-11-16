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
from typing import Any, Literal
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field

from eki.interview.models import Note, OpenItem, PlannedTopic
from eki.runbook import review
from eki.runbook.models import SECTION_NAMES, Claim, RunbookEntry, Section, SectionName
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
