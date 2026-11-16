"""F9: turn recorded notes into cited claims, without a model or new wording."""

from collections.abc import Sequence
from uuid import UUID, uuid5

from eki.ingest.redaction import RedactedText
from eki.interview.models import Note, OpenItem, PlannedTopic
from eki.runbook.models import (
    SECTION_NAMES,
    Claim,
    InterviewTurnCitation,
    RunbookEntry,
    Section,
    SectionName,
)
from eki.runbook.steps import claim_texts

CONFIDENTIAL = "Answered confidentially; not published."


def public_claims(
    session_id: UUID, topic_id: str, section: SectionName, notes: Sequence[Note]
) -> tuple[Claim, ...]:
    """Follow-up facts of one section: every active public note, each citing its own turn."""
    return tuple(
        Claim(
            text=text,
            citations=(InterviewTurnCitation(session_id=str(session_id), turn_index=n.turn),),
        )
        for n in notes
        if n.topic_id == topic_id
        and n.section == section
        and not n.superseded
        and not n.confidential
        for text in claim_texts(section, n.text)
        if text.strip()
    )


def replace_cited(
    name: SectionName,
    section: Section,
    session_id: UUID,
    corrected_turn: int,
    correction_turn: int,
    text: RedactedText,
) -> Section | None:
    """Replace the claims citing a corrected turn with the correction, cited to its turn.

    Claims with other sources stay as reviewed. ``None`` when nothing cites the turn.
    Entries and pending proposals share this single replacement rule.
    """
    claims: list[Claim] = []
    replaced = False
    for claim in section.claims:
        cites_target = any(
            isinstance(c, InterviewTurnCitation)
            and c.session_id == str(session_id)
            and c.turn_index == corrected_turn
            for c in claim.citations
        )
        if not cites_target:
            claims.append(claim)
            continue
        if not replaced:
            citation = InterviewTurnCitation(session_id=str(session_id), turn_index=correction_turn)
            claims.extend(
                Claim(text=step, citations=(citation,)) for step in claim_texts(name, text)
            )
        replaced = True
    if not replaced:
        return None
    return Section(claims=tuple(claims), open_question=section.open_question)


def build_entry(
    session_id: UUID, topic: PlannedTopic, notes: Sequence[Note], open_items: Sequence[OpenItem]
) -> RunbookEntry:
    """Build one complete draft. Its stable ID makes workflow retries safe."""
    active = {n.section: n for n in notes if n.topic_id == topic.topic_id and not n.superseded}
    opened = {o.section: o for o in open_items if o.topic_id == topic.topic_id}
    sections: dict[str, Section] = {}
    for name in SECTION_NAMES:
        note = active.get(name)
        if topic.origin == "follow_up":
            claims = public_claims(session_id, topic.topic_id, name, notes)
            if claims:
                sections[name] = Section(
                    claims=claims,
                    open_question=opened[name].reason if name in opened else None,
                )
                continue
        if note is not None:
            if note.confidential:
                sections[name] = Section(open_question=CONFIDENTIAL)
            else:
                texts = claim_texts(name, note.text)
                citation = InterviewTurnCitation(session_id=str(session_id), turn_index=note.turn)
                sections[name] = Section(
                    claims=tuple(Claim(text=t, citations=(citation,)) for t in texts if t.strip())
                )
        elif name in opened:
            sections[name] = Section(open_question=opened[name].reason)
    entry = RunbookEntry.model_validate(
        {
            "id": str(uuid5(session_id, topic.topic_id)),
            "topic_id": topic.topic_id,
            "title": topic.label,
            **sections,
        }
    )
    if entry.missing_sections():
        raise ValueError("topic has missing sections: " + ", ".join(entry.missing_sections()))
    return entry
