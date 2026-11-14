"""F10: edits and corrections preserve citations and require renewed approval."""

from uuid import UUID

import pytest

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
