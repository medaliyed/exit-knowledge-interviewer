"""F9: turn recorded notes into cited claims, without a model or new wording."""

from collections.abc import Sequence
from uuid import UUID, uuid5

from eki.interview.models import Note, OpenItem, PlannedTopic
from eki.runbook.models import (
    SECTION_NAMES,
    Claim,
    InterviewTurnCitation,
    RunbookEntry,
    Section,
)
from eki.runbook.steps import claim_texts

CONFIDENTIAL = "Answered confidentially; not published."


def build_entry(
    session_id: UUID, topic: PlannedTopic, notes: Sequence[Note], open_items: Sequence[OpenItem]
) -> RunbookEntry:
    """Build one complete draft. Its stable ID makes workflow retries safe."""
    active = {n.section: n for n in notes if n.topic_id == topic.topic_id and not n.superseded}
    opened = {o.section: o for o in open_items if o.topic_id == topic.topic_id}
    sections: dict[str, Section] = {}
    for name in SECTION_NAMES:
        note = active.get(name)
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
