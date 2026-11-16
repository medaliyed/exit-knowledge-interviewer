"""F22 9c P2: a proposal patch holds only selected sections with public follow-up facts."""

from uuid import UUID, uuid4

from eki.followup import proposals
from eki.ingest.redaction import RedactedText
from eki.interview.models import Note, OpenItem, PlannedTopic, Question
from eki.runbook.models import Claim, InterviewTurnCitation, RunbookEntry, Section
from eki.runbook.synthesis import CONFIDENTIAL

PARENT = UUID("00000000-0000-0000-0000-00000000000a")
FOLLOW_UP = UUID("00000000-0000-0000-0000-00000000000b")
TOPIC = "billing-close"
ROLLBACK_GAP, CONTACTS_GAP, PITFALLS_GAP, STEPS_GAP = (str(uuid4()) for _ in range(4))


def cited(text: str, turn: int = 2) -> Claim:
    return Claim(
        text=text,
        citations=(InterviewTurnCitation(session_id=str(PARENT), turn_index=turn),),
    )


def base() -> RunbookEntry:
    return RunbookEntry(
        id=str(uuid4()),
        topic_id=TOPIC,
        title="Billing close",
        trigger=Section(claims=(cited("Month end."),)),
        diagnosis=Section(claims=(cited("Totals differ."),)),
        steps=Section(claims=(cited("Run the close."),)),
        rollback=Section(open_question="I never had to roll it back"),
        contacts=Section(claims=(cited("Ask finance."),)),
        pitfalls=Section(open_question=CONFIDENTIAL),
        rationale=Section(claims=(cited("Audit."),)),
    )


def topic(*sections: str) -> PlannedTopic:
    gaps = {
        "rollback": ROLLBACK_GAP,
        "contacts": CONTACTS_GAP,
        "pitfalls": PITFALLS_GAP,
        "steps": STEPS_GAP,
    }
    return PlannedTopic(
        topic_id=TOPIC,
        label="Billing close",
        kind="procedure",
        system=None,
        origin="follow_up",
        score=None,
        reason="follow-up",
        artifact_ids=(),
        questions=tuple(
            Question(
                text=RedactedText(f"About {s}?"),
                origin="follow_up",
                gap_id=gaps[s],
                section=s,
            )
            for s in sections
        ),
        sections=sections,
        target_entry_id=str(uuid4()),
        base_version=3,
    )


def note(section: str, text: str, turn: int, **kwargs: bool) -> Note:
    return Note(
        topic_id=TOPIC,
        section=section,
        text=RedactedText(text),
        turn=turn,
        **kwargs,
    )


def test_build_patch_selected_sections_only() -> None:
    notes = [
        note("rollback", "Restore the pre-run export.", 2),
        note("contacts", "Finance operations lead authorises it.", 4),
        note("contacts", "The CFO signs above the threshold.", 6),
        note("steps", "Out of scope.", 6),
        note("contacts", "Superseded fact.", 3, superseded=True),
        note("contacts", "Secret.", 8, confidential=True),
    ]
    outcomes = {ROLLBACK_GAP: "answered", CONTACTS_GAP: "answered"}
    patch = proposals.build_patch(
        FOLLOW_UP, topic("rollback", "contacts"), notes, [], outcomes, base()
    )
    assert set(patch) == {"rollback", "contacts"}, "unselected sections are absent"
    rollback = patch["rollback"]
    assert rollback.clears_open is True and rollback.gap_ids == (ROLLBACK_GAP,)
    assert [c.text for c in rollback.claims] == ["Restore the pre-run export."]
    contacts = patch["contacts"]
    assert [c.text for c in contacts.claims] == [
        "Finance operations lead authorises it.",
        "The CFO signs above the threshold.",
    ]
    turns = [c.citations[0] for c in contacts.claims]
    assert [t.turn_index for t in turns] == [4, 6]  # type: ignore[union-attr]
    assert all(
        isinstance(c, InterviewTurnCitation) and c.session_id == str(FOLLOW_UP)
        for section in patch.values()
        for claim in section.claims
        for c in claim.citations
    ), "citations carry the follow-up session id"


def test_unanswered_outcomes_give_no_patch() -> None:
    for outcome in ("skipped", "unknown", "confidential", "unattempted", "answered_unnoted"):
        patch = proposals.build_patch(
            FOLLOW_UP, topic("rollback"), [], [], {ROLLBACK_GAP: outcome}, base()
        )
        assert patch == {}, outcome
    confidential = [note("rollback", "Secret.", 2, confidential=True)]
    assert (
        proposals.build_patch(
            FOLLOW_UP, topic("rollback"), confidential, [], {ROLLBACK_GAP: "answered"}, base()
        )
        == {}
    )


def test_partial_answers_keep_the_open_question() -> None:
    notes = [note("rollback", "Restore the export.", 2)]
    mixed = proposals.build_patch(
        FOLLOW_UP,
        topic("rollback"),
        notes,
        [],
        {ROLLBACK_GAP: "unknown"},
        base(),
    )
    assert mixed["rollback"].clears_open is False, "answered plus unknown keeps the question"
    still_open = [
        OpenItem(topic_id=TOPIC, section="rollback", reason=RedactedText("Not sure"), turn=2)
    ]
    blocked = proposals.build_patch(
        FOLLOW_UP, topic("rollback"), notes, still_open, {ROLLBACK_GAP: "answered"}, base()
    )
    assert blocked["rollback"].clears_open is False, "an open item blocks clearing"
    marker = proposals.build_patch(
        FOLLOW_UP,
        topic("pitfalls"),
        [note("pitfalls", "Public pitfall.", 2)],
        [],
        {PITFALLS_GAP: "answered"},
        base(),
    )
    assert marker["pitfalls"].clears_open is False, "the confidential marker is never cleared"


def test_selected_merges_or_replaces_only_chosen_sections() -> None:
    notes = [
        note("rollback", "Restore the export.", 2),
        note("contacts", "Finance lead.", 4),
    ]
    outcomes = {ROLLBACK_GAP: "answered", CONTACTS_GAP: "answered"}
    original = base()
    patch = proposals.build_patch(
        FOLLOW_UP, topic("rollback", "contacts"), notes, [], outcomes, original
    )
    merged = proposals.selected(original, patch, {"rollback": "merge"})
    assert merged.rollback.open_question is None
    assert [c.text for c in merged.rollback.claims] == ["Restore the export."]
    assert merged.contacts == original.contacts, "an unselected section is untouched"
    assert merged.id == original.id and merged.status == "draft"
    kept = proposals.selected(original, patch, {"contacts": "merge"})
    assert [c.text for c in kept.contacts.claims] == ["Ask finance.", "Finance lead."]
    replaced = proposals.selected(original, patch, {"contacts": "replace"})
    assert [c.text for c in replaced.contacts.claims] == ["Finance lead."]
    for name in ("trigger", "diagnosis", "steps", "pitfalls", "rationale"):
        assert replaced.section(name) == original.section(name)
