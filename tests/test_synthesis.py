"""F9: deterministic synthesis preserves evidence and never invents missing knowledge."""

from uuid import uuid4

import pytest

from eki.ingest.redaction import RedactedText
from eki.interview.models import Note, OpenItem
from eki.runbook.models import SECTION_NAMES, EntryStatus, InterviewTurnCitation
from eki.runbook.synthesis import build_entry
from tests.test_interview_models import topic


def test_synthesis_is_repeatable() -> None:
    session = uuid4()
    notes = [
        Note(topic_id="t", section=s, text=RedactedText("First. Second!"), turn=2)
        for s in SECTION_NAMES
    ]
    notes.append(notes[0].model_copy(update={"superseded": True, "text": "outdated"}))
    entry = build_entry(session, topic("t"), notes, [])
    assert entry == build_entry(session, topic("t"), notes, [])
    assert entry.status == EntryStatus.DRAFT
    assert not entry.missing_sections()
    assert [c.text for c in entry.steps.claims] == ["First.", "Second!"]
    for section in SECTION_NAMES:
        for claim in entry.section(section).claims:
            assert claim.citations == (
                InterviewTurnCitation(session_id=str(session), turn_index=2),
            )
    assert "outdated" not in entry.model_dump_json()


def test_open_and_confidential_sections() -> None:
    notes = [
        Note(
            topic_id="t",
            section="contacts",
            text=RedactedText("private answer"),
            turn=2,
            confidential=True,
        )
    ]
    items = [
        OpenItem(topic_id="t", section=s, reason=RedactedText("I don't remember"), turn=3)
        for s in SECTION_NAMES
        if s != "contacts"
    ]
    entry = build_entry(uuid4(), topic("t"), notes, items)
    assert entry.contacts.open_question == "Answered confidentially; not published."
    assert "private answer" not in entry.model_dump_json()
    assert not entry.missing_sections()
    assert entry.steps.open_question == "I don't remember"


def test_incomplete_topic_is_refused() -> None:
    with pytest.raises(ValueError, match="missing"):
        build_entry(uuid4(), topic("t"), [], [])
