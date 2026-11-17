"""Task F8: the expert can skip a question, answer confidentially, and correct an answer."""

import pytest

from eki.interview.service import NotFoundError
from tests.conftest import Scratch
from tests.fakes import tool_reply
from tests.interview_support import say
from tests.support import tables_containing
from tests.test_interviewer import ask, note, start, tool_results

pytestmark = [pytest.mark.db, pytest.mark.anyio]
VENDOR_CONTACT = "Marek Dolan at the certificate vendor"


async def test_skip_marks_the_section_open_and_moves_on(scratch: Scratch) -> None:
    service, model, view = await start(
        scratch,
        [tool_reply(ask("Who do you call?", "contacts")), tool_reply(ask("What triggers it?"))],
    )
    events = [e async for e in await service.post(view.session.id, "skip")]
    assert [e["type"] for e in events] == ["open", "question"]
    assert events[0] == {
        "type": "open",
        "topic_id": events[-1]["topic_id"],
        "section": "contacts",
        "reason": "skipped by expert",
    }
    assert events[-1]["question"] == "What triggers it?"
    current = await service.view(view.session.id)
    assert [(o.section, o.reason, o.turn) for o in current.open_items] == [
        ("contacts", "skipped by expert", 2)
    ]
    assert [(t.kind, t.text) for t in current.turns][1] == ("skip", "(skipped)")
    assert current.checklist["contacts"] == "open"
    told = tool_results(model, 1)[-1]
    assert told.startswith("The expert skipped this question.")
    assert "contacts" in told


async def test_skip_of_an_already_noted_section_keeps_the_note(scratch: Scratch) -> None:
    service, _, view = await start(
        scratch,
        [
            tool_reply(ask("What triggers it?")),
            tool_reply(note("trigger"), ask("Anything else on the trigger?")),
            tool_reply(ask("Steps?", "steps")),
        ],
    )
    await say(service, view.session.id, "The batch aborts.")
    [e async for e in await service.post(view.session.id, "skip")]
    current = await service.view(view.session.id)
    assert current.checklist["trigger"] == "noted"
    assert current.open_items == ()


async def test_confidential_answer_is_stored_flagged_and_never_shown_to_the_model(
    scratch: Scratch,
) -> None:
    service, model, view = await start(
        scratch,
        [
            tool_reply(ask("Who do you call?", "contacts")),
            tool_reply(note("contacts", "invented"), ask("What triggers it?")),
            tool_reply(note("trigger"), ask("Steps?", "steps")),
        ],
    )
    events = await say(service, view.session.id, VENDOR_CONTACT, confidential=True)
    assert events[0] == {
        "type": "note",
        "topic_id": events[-1]["topic_id"],
        "section": "contacts",
        "confidential": True,
    }
    await say(service, view.session.id, "The batch aborts.")
    current = await service.view(view.session.id)
    flagged = current.turns[1]
    assert (flagged.confidential, flagged.text) == (True, VENDOR_CONTACT)
    assert current.checklist["contacts"] == "confidential", "the section counts as answered"
    (confidential_note,) = [n for n in current.notes if n.section == "contacts"]
    assert confidential_note.confidential is True
    assert VENDOR_CONTACT not in confidential_note.text
    assert "Marek" not in model.sent_text(), "absent from every model call, then and later"
    told = tool_results(model, 2)
    assert told[0].startswith("The expert answered confidentially.")
    assert told[1].startswith("Refused: contacts was answered confidentially")
    with scratch.connect() as conn:
        assert tables_containing(conn, scratch.schema, "Marek") == ["turns.text"]


async def test_correction_supersedes_the_note_and_keeps_both_turns(scratch: Scratch) -> None:
    service, model, view = await start(
        scratch,
        [
            tool_reply(ask("In which order do you restart?", "steps")),
            tool_reply(note("steps", "Workers first, then replicas."), ask("What triggers it?")),
            tool_reply(note("trigger", "The batch aborts."), ask("Rollback?", "rollback")),
        ],
    )
    session_id = view.session.id
    await say(service, session_id, "Workers first, then the replicas.")
    events = [
        e
        async for e in await service.post(
            session_id, "correct", "Replicas first, then the workers.", corrects=2
        )
    ]
    assert [e["type"] for e in events] == ["corrected", "question"]
    assert events[0] == {"type": "corrected", "turn": 4, "corrects": 2, "sections": ["steps"]}
    assert events[-1]["question"] == "What triggers it?", "the open question is asked again"
    assert len(model.calls) == 2, "a correction needs no model call"
    current = await service.view(session_id)
    assert [(t.ordinal, t.kind, t.corrects) for t in current.turns] == [
        (1, "question", None),
        (2, "answer", None),
        (3, "question", None),
        (4, "correction", 2),
    ]
    assert current.turns[1].text == "Workers first, then the replicas.", "the old turn is kept"
    assert [(n.section, n.text, n.turn) for n in current.notes] == [
        ("steps", "Replicas first, then the workers.", 4)
    ]
    await say(service, session_id, "The batch aborts.")
    answer_result = tool_results(model, 2)[-1]
    assert "corrected an earlier answer" in answer_result
    assert "Replicas first, then the workers." in answer_result
    assert "The expert answered:\nThe batch aborts." in answer_result
    turns = (await service.view(session_id)).turns
    assert [(t.kind, t.answers) for t in turns][-2:] == [("answer", 3), ("question", None)]


async def test_correcting_a_turn_that_does_not_exist_is_refused(scratch: Scratch) -> None:
    service, _, view = await start(scratch, [tool_reply(ask("What triggers it?"))])
    session_id = view.session.id
    for target in (99, 1, None):
        with pytest.raises((NotFoundError, ValueError)):
            await service.post(session_id, "correct", "Replicas first.", corrects=target)
    with pytest.raises(ValueError, match="text"):
        await service.post(session_id, "correct", "  ", corrects=1)
    assert len((await service.view(session_id)).turns) == 1, "nothing was stored"


async def test_a_correction_is_redacted_like_any_answer(scratch: Scratch) -> None:
    service, _, view = await start(
        scratch,
        [
            tool_reply(ask("How do you log in?", "steps")),
            tool_reply(note("steps", "With the shared account."), ask("What triggers it?")),
        ],
    )
    await say(service, view.session.id, "With the shared account.")
    [
        e
        async for e in await service.post(
            view.session.id, "correct", "Use token=FAKE-TOKEN-3b9f6e2a8c4d1075 instead.", corrects=2
        )
    ]
    current = await service.view(view.session.id)
    assert current.turns[-1].text == "Use token=[REDACTED:credential] instead."
    with scratch.connect() as conn:
        assert tables_containing(conn, scratch.schema, "FAKE-TOKEN-3b9f6e2a8c4d1075") == []


async def test_traces_carry_ids_and_never_a_confidential_answer(scratch: Scratch) -> None:
    """Task P2-i: every model call is traced under its run and session; secrets never are."""
    from eki.graph.workflow import Limits
    from tests.fakes import RecordingTracer, ScriptedModel
    from tests.interview_support import make_service, seed_run
    from tests.support import FAKE_CREDENTIALS

    with scratch.connect() as conn:
        run_id = seed_run(conn)
    tracer = RecordingTracer()
    script = [
        tool_reply(ask("Who do you call?", "contacts")),
        tool_reply(ask("How do you log in?", "steps")),
        tool_reply(note("steps", "With the shared account."), ask("What triggers it?")),
    ]
    service = make_service(
        scratch.dsn,
        agent=None,
        interviewer=ScriptedModel(script, name="interviewer"),
        limits=Limits(plan_size=1),
        tracer=tracer,
    )
    plan = await service.draft_plan(run_id)
    view = await service.approve(plan.id, "Nadia Ferrand", expected_revision=plan.revision)
    await say(service, view.session.id, VENDOR_CONTACT, confidential=True)
    await say(
        service, view.session.id, f"With password={FAKE_CREDENTIALS[1]} on the shared account."
    )
    assert [(c.step, c.model) for c in tracer.calls] == [
        ("draft", "drafter"),
        ("interview", "interviewer"),
        ("interview", "interviewer"),
        ("interview", "interviewer"),
    ]
    assert {c.run_id for c in tracer.calls} == {str(run_id)}
    assert {c.plan_id for c in tracer.calls} == {str(plan.id)}
    assert [c.session_id for c in tracer.calls] == [None, *[str(view.session.id)] * 3]
    traced = tracer.text()
    assert "Marek" not in traced
    assert FAKE_CREDENTIALS[1] not in traced
    assert "password=[REDACTED:credential]" in traced
