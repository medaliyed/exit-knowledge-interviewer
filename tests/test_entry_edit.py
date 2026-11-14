"""F10: edits and corrections preserve citations and require renewed approval."""

from uuid import UUID

import pytest

from eki.errors import NotFoundError
from eki.interview import store as interviews
from eki.runbook.review import SectionEdit
from eki.runbook.service import RunbookService
from tests.conftest import Scratch
from tests.interview_support import make_service, say
from tests.support import FAKE_CREDENTIALS, tables_containing
from tests.test_workflow_synthesis import finished

pytestmark = [pytest.mark.db, pytest.mark.anyio]
EXPERT = "Nadia Ferrand"


async def test_edit_is_a_cited_expert_turn_and_reopens_decisions(scratch: Scratch) -> None:
    session = await finished(scratch)
    service = RunbookService(scratch.dsn)
    first = service.list(session)[0]
    entry_id = UUID(first.entry.id)
    approved = service.approve(entry_id, EXPERT)
    edited = service.edit(entry_id, EXPERT, {"steps": SectionEdit(claims=("Restart twice.",))})
    assert edited.entry.status == "draft"
    assert edited.version == approved.version + 1
    assert edited.entry.approved_by is None
    claim = edited.entry.steps.claims[0]
    with scratch.connect() as conn:
        turn = interviews.load_turn(conn, session, claim.citations[0].turn_index)  # type: ignore[union-attr]
        assert turn is not None and (turn.kind, turn.speaker, turn.text) == (
            "edit",
            "expert",
            "steps: Restart twice.",
        )
    service.reject(entry_id, EXPERT, "Needs work")
    again = service.edit(
        entry_id, EXPERT, {"steps": SectionEdit(open_question="Ask the maintainer")}
    )
    assert again.entry.status == "draft" and again.rejection_reason is None
    assert service.approve(entry_id, EXPERT).entry.status == "approved"


async def test_edit_cannot_leave_a_section_empty(scratch: Scratch) -> None:
    session = await finished(scratch)
    service = RunbookService(scratch.dsn)
    entry_id = UUID(service.list(session)[0].entry.id)
    with pytest.raises(ValueError):
        service.edit(entry_id, EXPERT, {"steps": SectionEdit()})
    with pytest.raises(ValueError):
        service.edit(entry_id, EXPERT, {})
    assert service.history(entry_id) == ()
    assert service.get(entry_id).version == 1


async def test_correction_reopens_an_approved_entry(scratch: Scratch) -> None:
    session = await finished(scratch)
    service = RunbookService(scratch.dsn)
    first, untouched, _ = service.list(session)
    entry_id = UUID(first.entry.id)
    service.approve(entry_id, EXPERT)
    other = service.approve(UUID(untouched.entry.id), EXPERT)
    await say(
        make_service(scratch.dsn),
        session,
        "Use the corrected procedure.",
        action="correct",
        corrects=2,
    )
    corrected = service.get(entry_id)
    assert corrected.entry.status == "draft"
    assert corrected.entry.steps.claims[0].text == "Use the corrected procedure."
    assert service.get(UUID(untouched.entry.id)) == other
    assert service.history(entry_id)[-1].action == "correction"
    assert service.approve(entry_id, EXPERT).entry.status == "approved"


async def test_edit_and_reason_are_redacted(scratch: Scratch) -> None:
    session = await finished(scratch)
    service = RunbookService(scratch.dsn)
    entry_id = UUID(service.list(session)[0].entry.id)
    leak = f"password={FAKE_CREDENTIALS[1]}"
    service.edit(entry_id, EXPERT, {"steps": SectionEdit(claims=(f"Use {leak}",))})
    service.reject(entry_id, EXPERT, f"Remove {leak}")
    with scratch.connect() as conn:
        assert tables_containing(conn, scratch.schema, FAKE_CREDENTIALS[1]) == []
    assert "[REDACTED:credential]" in service.get(entry_id).entry.model_dump_json()


async def test_correction_of_uncited_turn_keeps_approval(scratch: Scratch) -> None:
    from eki.ingest.redaction import RedactedText

    session = await finished(scratch)
    service = RunbookService(scratch.dsn)
    entry_id = UUID(service.list(session)[0].entry.id)
    approved = service.approve(entry_id, EXPERT)
    with scratch.connect() as conn:
        unused = interviews.add_expert_turn(
            conn, session, approved.entry.topic_id, "answer", RedactedText("Unrecorded detail")
        )
    await say(
        make_service(scratch.dsn), session, "Corrected detail.", action="correct", corrects=unused
    )
    assert service.get(entry_id) == approved


async def test_synthesis_keeps_an_edited_draft(scratch: Scratch) -> None:
    """Regression: re-running synthesis used to replace the expert's unapproved edits."""
    session = await finished(scratch)
    service = RunbookService(scratch.dsn)
    first, second, _ = service.list(session)
    assert (first.edited, second.edited) == (False, False)
    entry_id = UUID(first.entry.id)
    service.approve(entry_id, EXPERT)
    edited = service.edit(entry_id, EXPERT, {"steps": SectionEdit(claims=("Restart twice.",))})
    assert edited.edited is True
    also_edited = service.edit(
        UUID(second.entry.id), EXPERT, {"rollback": SectionEdit(open_question="Ask finance.")}
    )

    rebuilt = service.synthesise(session)
    assert rebuilt[0] == edited, "content, version and status are untouched"
    assert rebuilt[0].entry.steps.claims[0].text == "Restart twice."
    assert rebuilt[1] == also_edited, "an edit with only an open question is kept too"
    assert rebuilt[2].edited is False
    assert service.get(entry_id).edited is True


async def test_synthesis_overwrites_edits_only_when_asked(scratch: Scratch) -> None:
    session = await finished(scratch)
    service = RunbookService(scratch.dsn)
    original = service.list(session)[0]
    entry_id = UUID(original.entry.id)
    edited = service.edit(entry_id, EXPERT, {"steps": SectionEdit(claims=("Restart twice.",))})

    rebuilt = service.synthesise(session, overwrite_edits=True)[0]
    assert rebuilt.entry == original.entry, "back to what the notes say"
    assert rebuilt.edited is False
    assert rebuilt.version == edited.version + 1
    with scratch.connect() as conn:
        kinds = [t.kind for t in interviews.load_turns(conn, session)]
    assert kinds.count("edit") == 1, "the edit stays in the transcript"
    assert [d.action for d in service.history(entry_id)] == ["edit"]
    again = service.synthesise(session)[0]
    assert (again.entry, again.edited) == (rebuilt.entry, False), (
        "unedited drafts rebuild as before"
    )


async def test_approval_keeps_the_edit_and_a_later_synthesis_leaves_it(scratch: Scratch) -> None:
    session = await finished(scratch)
    service = RunbookService(scratch.dsn)
    entry_id = UUID(service.list(session)[0].entry.id)
    service.edit(entry_id, EXPERT, {"steps": SectionEdit(claims=("Restart twice.",))})
    approved = service.approve(entry_id, EXPERT)
    assert service.synthesise(session, overwrite_edits=True)[0] == approved
    assert approved.entry.steps.claims[0].text == "Restart twice."


async def test_review_is_told_what_the_cited_answer_does_not_say(scratch: Scratch) -> None:
    """F9: the stand-in interviewer notes "noted"; the answer "Recorded answer." never says it."""
    session = await finished(scratch)
    service = RunbookService(scratch.dsn)
    first, second, third = service.list(session)
    found = service.unsupported(session)
    assert list(found) == [first.entry.id, second.entry.id, third.entry.id]
    assert [(u.section, u.index, u.terms) for u in found[first.entry.id]] == [
        (name, 0, ("noted",))
        for name in ("trigger", "diagnosis", "steps", "rollback", "contacts", "pitfalls")
    ] + [("rationale", 0, ("noted",))]

    service.edit(
        UUID(first.entry.id),
        EXPERT,
        {"steps": SectionEdit(claims=("Restart twice.", "Then wait 15 minutes."))},
    )
    after = service.unsupported(session)
    assert "steps" not in [u.section for u in after[first.entry.id]], "an edit is its own source"
    assert len(after[first.entry.id]) == 6 and len(after[second.entry.id]) == 7

    await say(
        make_service(scratch.dsn),
        session,
        "Use the corrected procedure.",
        action="correct",
        corrects=4,
    )
    assert service.unsupported(session)[second.entry.id] == (), "so is a correction"
    with pytest.raises(NotFoundError):
        service.unsupported(UUID(int=0))


@pytest.mark.parametrize(
    ("said", "steps"),
    [
        (
            "Stop the worker. Check the queue, e.g. the export queue. Start the worker.",
            ["Stop the worker.", "Check the queue, e.g. the export queue.", "Start the worker."],
        ),
        (
            "1. Stop the worker. Wait. 2. Check the queue. 3. Start the worker.",
            ["Stop the worker. Wait.", "Check the queue.", "Start the worker."],
        ),
        (
            "- Stop the worker. Wait.\n- Check the queue.\n- Start the worker.",
            ["Stop the worker. Wait.", "Check the queue.", "Start the worker."],
        ),
    ],
)
async def test_a_correction_keeps_the_procedure_as_steps(
    scratch: Scratch, said: str, steps: list[str]
) -> None:
    """Regression: a correction after synthesis used to merge every step into one claim."""
    session = await finished(scratch)
    service = RunbookService(scratch.dsn)
    entry_id = UUID(service.list(session)[0].entry.id)
    service.approve(entry_id, EXPERT)
    events = await say(make_service(scratch.dsn), session, said, action="correct", corrects=2)

    corrected = service.get(entry_id).entry
    assert corrected.status == "draft"
    assert [c.text for c in corrected.steps.claims] == steps
    assert len({c.citations for c in corrected.steps.claims}) == 1, "all cite the correction"
    assert [c.text for c in corrected.trigger.claims] == [said], "other sections stay whole"
    rebuilt = service.synthesise(session)[0].entry
    assert rebuilt.steps == corrected.steps, "the same steps whichever way they were built"
    assert service.unsupported(session)[str(entry_id)] == ()

    # Correcting several claims from the same turn replaces the procedure just once.
    service.approve(entry_id, EXPERT)
    await say(
        make_service(scratch.dsn),
        session,
        "Pause the worker. Resume the worker.",
        action="correct",
        corrects=events[0]["turn"],
    )
    again = service.get(entry_id).entry
    assert again.status == "draft"
    assert [c.text for c in again.steps.claims] == ["Pause the worker.", "Resume the worker."]
    assert service.synthesise(session)[0].entry.steps == again.steps
    assert service.approve(entry_id, EXPERT).entry.status == "approved"
