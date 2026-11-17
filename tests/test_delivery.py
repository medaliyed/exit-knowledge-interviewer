"""F14: durable, question-bound submissions survive interrupted processing."""

from typing import Any
from uuid import UUID, uuid4

import pytest

from eki.errors import ConflictError
from eki.graph.workflow import Deps, InterviewState
from eki.interview import store
from eki.interview.service import Action, InterviewService, SessionView
from eki.llm import ModelError
from eki.runbook.service import RunbookService
from tests.conftest import Scratch
from tests.interview_support import make_service, one_question_agent, say, seed_run
from tests.test_workflow_synthesis import finished

pytestmark = [pytest.mark.db, pytest.mark.anyio]


async def opened(scratch: Scratch) -> tuple[InterviewService, SessionView]:
    service = make_service(scratch.dsn)
    with scratch.connect() as conn:
        run = seed_run(conn)
    plan = await service.draft_plan(run)
    return service, await service.approve(plan.id, "Nadia Ferrand", expected_revision=plan.revision)


@pytest.mark.parametrize(
    "action,confidential", [("answer", False), ("answer", True), ("skip", False)]
)
async def test_saved_before_stream_and_retry_after_disconnect(
    scratch: Scratch, action: Action, confidential: bool
) -> None:
    service, view = await opened(scratch)
    sid, mid = view.session.id, uuid4()
    stream = await service.post(
        sid, action, "Recorded answer.", message_id=mid, question_turn=1, confidential=confidential
    )
    assert (await service.receipt(sid, mid))["status"] == "saved"
    # Close without running the workflow: acceptance has already committed.
    await stream.aclose()
    service = make_service(scratch.dsn)
    recovered = await service.resume(sid, mid)
    events = [e async for e in recovered]
    assert events[-1]["type"] == "question" and events[-1]["turn"] == 3
    assert (await service.receipt(sid, mid))["status"] == "processed"
    again = await say(
        service,
        sid,
        "Recorded answer.",
        action=action,
        message_id=mid,
        question_turn=1,
        confidential=confidential,
    )
    assert again[-1] == events[-1]
    assert len((await service.view(sid)).turns) == 3
    assert "Recorded answer." not in str(await service.receipt(sid, mid))


async def test_id_and_target_conflicts_never_replace_a_saved_answer(scratch: Scratch) -> None:
    service, view = await opened(scratch)
    sid, mid = view.session.id, uuid4()
    await say(service, sid, "Original.", message_id=mid, question_turn=1)
    for options in (
        {"text": "Changed."},
        {"text": "Original.", "confidential": True},
        {"text": "Original.", "question_turn": 3},
    ):
        args: dict[str, Any] = {"message_id": mid, "question_turn": 1, **options}
        with pytest.raises(ConflictError):
            await service.post(sid, **args)
    with pytest.raises(ConflictError):
        await service.post(sid, text="Late.", message_id=uuid4(), question_turn=1)
    assert (await service.view(sid)).turns[1].text == "Original."


async def test_model_failure_retries_the_saved_message_without_new_text(scratch: Scratch) -> None:
    service, view = await opened(scratch)
    failed = {"on": True}

    async def flaky(deps: Deps, state: InterviewState) -> dict[str, Any]:
        if failed["on"]:
            raise ModelError("down")
        return await one_question_agent(deps, state)

    service.agent = flaky
    sid, mid = view.session.id, uuid4()
    events = await say(service, sid, "Keep me.", message_id=mid, question_turn=1)
    assert events[-1]["type"] == "error"
    assert (await service.receipt(sid, mid))["status"] == "needs_retry"
    failed["on"] = False
    stream = await service.resume(sid, mid)
    assert [e async for e in stream][-1]["turn"] == 3
    assert [t.text for t in (await service.view(sid)).turns if t.speaker == "expert"] == [
        "Keep me."
    ]


async def test_answer_saved_before_its_interrupt_checkpoint(scratch: Scratch) -> None:
    fail = {"on": True}

    async def before_checkpoint(deps: Deps, state: InterviewState) -> dict[str, Any]:
        result = await one_question_agent(deps, state)
        if fail["on"]:
            raise RuntimeError("died after question commit")
        return result

    service = make_service(scratch.dsn, agent=before_checkpoint)
    with scratch.connect() as conn:
        run = seed_run(conn)
    plan = await service.draft_plan(run)
    with pytest.raises(RuntimeError):
        await service.approve(plan.id, "Nadia Ferrand", expected_revision=plan.revision)
    with scratch.connect() as conn:
        sid = store.session_of_plan(conn, plan.id)
    assert sid is not None
    fail["on"] = False
    mid = uuid4()
    events = await say(service, sid, "Still saved.", message_id=mid, question_turn=1)
    assert events[-1]["turn"] == 3
    assert (await service.receipt(sid, mid))["status"] == "processed"
    assert [t.text for t in (await service.view(sid)).turns if t.speaker == "expert"] == [
        "Still saved."
    ]


async def test_correction_retry_does_not_repeat_review_effects(scratch: Scratch) -> None:
    sid = await finished(scratch)
    service = make_service(scratch.dsn)
    books = RunbookService(scratch.dsn)
    entry_id = books.list(sid)[0].entry.id

    books.approve(UUID(entry_id), "Nadia Ferrand")
    mid = uuid4()
    await say(service, sid, "Stop it. Start it.", action="correct", corrects=2, message_id=mid)
    changed = books.get(UUID(entry_id))
    history = books.history(UUID(entry_id))
    await say(service, sid, "Stop it. Start it.", action="correct", corrects=2, message_id=mid)
    assert books.get(UUID(entry_id)) == changed
    assert books.history(UUID(entry_id)) == history


async def test_crash_after_tool_write_reuses_saved_reply(
    scratch: Scratch, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.fakes import ScriptedModel, tool_reply
    from tests.test_interviewer import ask, note

    model = ScriptedModel(
        [
            tool_reply(ask("Trigger?")),
            tool_reply(note("trigger", "Recorded."), ask("Steps?", "steps")),
        ]
    )
    service = make_service(scratch.dsn, agent=None, interviewer=model)
    with scratch.connect() as conn:
        run = seed_run(conn)
    plan = await service.draft_plan(run)
    view = await service.approve(plan.id, "Nadia Ferrand", expected_revision=plan.revision)
    original = store.record_note

    def crash(*args: Any, **kwargs: Any) -> None:
        original(*args, **kwargs)
        raise RuntimeError("died after tool commit")

    monkeypatch.setattr(store, "record_note", crash)
    mid = uuid4()
    with pytest.raises(RuntimeError):
        await say(service, view.session.id, "Recorded.", message_id=mid, question_turn=1)
    monkeypatch.setattr(store, "record_note", original)
    restarted = make_service(scratch.dsn, agent=None, interviewer=model)
    events = [e async for e in await restarted.resume(view.session.id, mid)]
    assert events[-1]["turn"] == 3
    assert len(model.calls) == 2, "saved replies are replayed without calling the model again"
    with scratch.connect() as conn:
        assert conn.execute("SELECT count(*) FROM notes").fetchone() == (1,)


def test_http_receipt_and_resume(scratch: Scratch) -> None:
    from tests.test_api_interview import Api

    api = Api(scratch)
    session = api.approve(api.plan()["id"])
    mid = str(uuid4())
    url = f"/sessions/{session['id']}/messages"
    response = api.client.post(
        url, json={"message_id": mid, "question_turn": 1, "text": "Recorded."}
    )
    assert response.status_code == 200
    assert response.headers["x-eki-message-id"] == mid
    receipt = api.client.get(f"{url}/{mid}").json()
    assert receipt["status"] == "processed"
    assert "Recorded." not in str(receipt)
    assert api.client.post(f"{url}/{mid}/resume").status_code == 200
    assert len(api.client.get(f"/sessions/{session['id']}").json()["turns"]) == 3


@pytest.mark.parametrize("after_note", [False, True])
async def test_actual_process_death_keeps_the_answer(scratch: Scratch, after_note: bool) -> None:
    import asyncio
    import json
    import subprocess
    import sys

    service, view = await opened(scratch)
    sid, mid = view.session.id, uuid4()
    script = """
import asyncio, json, os, sys
from uuid import UUID
from eki.interview import store
from tests.interview_support import make_service
values = json.load(sys.stdin)
original = store.record_note
def crash(*args, **kwargs):
    original(*args, **kwargs)
    os._exit(0)
if values['after_note']:
    store.record_note = crash
async def main():
    stream = await make_service(values['dsn']).post(
        UUID(values['session']), text='Saved across process death.',
        message_id=UUID(values['message']), question_turn=1)
    if not values['after_note']:
        os._exit(0)
    async for event in stream:
        pass
asyncio.run(main())
"""
    await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-c", script],
        check=True,
        timeout=30,
        input=json.dumps(
            {"dsn": scratch.dsn, "session": str(sid), "message": str(mid), "after_note": after_note}
        ),
        text=True,
        capture_output=True,
    )
    assert (await service.receipt(sid, mid))["status"] == ("needs_retry" if after_note else "saved")
    events = [e async for e in await service.resume(sid, mid)]
    assert events[-1]["turn"] == 3
    current = await service.view(sid)
    assert [t.text for t in current.turns if t.speaker == "expert"] == [
        "Saved across process death."
    ]
    with scratch.connect() as conn:
        assert conn.execute("SELECT count(*) FROM notes").fetchone() == (7,)
