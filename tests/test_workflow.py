"""Task F7: the workflow stops for review, runs topics in order and survives a restart."""

from typing import Any
from uuid import UUID, uuid4

import pytest

from eki.graph.workflow import Deps, InterviewState, open_workflow, thread_config
from eki.ingest.redaction import RedactedText
from eki.interview import store
from eki.interview.plan import PlanFrozenError, remove_topic, reorder
from eki.interview.service import BusyError, ConflictError, NotFoundError
from eki.llm import ModelError
from tests.conftest import Scratch
from tests.fakes import ScriptedModel
from tests.interview_support import (
    TOPIC_IDS,
    make_service,
    one_question_agent,
    quoting_drafter,
    say,
    seed_run,
)

pytestmark = [pytest.mark.db, pytest.mark.anyio]


@pytest.fixture
def run_id(scratch: Scratch) -> UUID:
    with scratch.connect() as conn:
        return seed_run(conn)


async def test_graph_stops_for_plan_review(scratch: Scratch, run_id: UUID) -> None:
    service = make_service(scratch.dsn)
    plan = await service.draft_plan(run_id)
    assert plan.status == "draft"
    assert [t.topic_id for t in plan.topics] == list(TOPIC_IDS), "highest gap score first"
    for topic in plan.topics:
        (question,) = topic.questions
        assert question.artifact_id in topic.artifact_ids
        assert question.quote
    with scratch.connect() as conn:
        assert store.session_of_plan(conn, plan.id) is None
        async with open_workflow(scratch.dsn, service._deps(conn), one_question_agent) as workflow:
            snapshot = await workflow.aget_state(thread_config(plan.id))
    assert snapshot.next == ("await_review",)
    assert [i.value["type"] for i in snapshot.interrupts] == ["plan_review"]
    assert await service.get_plan(plan.id) == plan


async def test_resume_after_approval_starts_first_topic(scratch: Scratch, run_id: UUID) -> None:
    service = make_service(scratch.dsn)
    plan = await service.draft_plan(run_id)
    view = await service.approve(plan.id, "Nadia Ferrand")
    approved = await service.get_plan(plan.id)
    assert (approved.status, approved.approved_by) == ("approved", "Nadia Ferrand")
    assert (view.session.status, view.session.current_topic_id) == ("active", TOPIC_IDS[0])
    assert view.waiting is not None
    assert (view.waiting.ordinal, view.waiting.topic_id) == (1, TOPIC_IDS[0])
    assert set(view.checklist.values()) == {"missing"}


async def test_topics_run_in_plan_order(scratch: Scratch, run_id: UUID) -> None:
    service = make_service(scratch.dsn)
    plan = await service.draft_plan(run_id)
    a, b, c = TOPIC_IDS
    await service.edit_plan(plan.id, lambda p: reorder(remove_topic(p, b), [c, a]))
    view = await service.approve(plan.id, "Nadia Ferrand")
    asked = [view.waiting.topic_id if view.waiting else None]
    events = await say(service, view.session.id, "First answer.")
    assert [e["type"] for e in events] == ["topic", "question"]
    assert events[0] == {"type": "topic", "topic_id": a, "label": "Topic 1", "index": 2, "total": 2}
    asked.append(events[-1]["topic_id"])
    assert asked == [c, a]


async def test_graph_ends_when_every_topic_is_complete(scratch: Scratch, run_id: UUID) -> None:
    service = make_service(scratch.dsn)
    plan = await service.draft_plan(run_id)
    view = await service.approve(plan.id, "Nadia Ferrand")
    session_id = view.session.id
    last: list[dict[str, Any]] = []
    for n in range(3):
        last = await say(service, session_id, f"Answer {n}.")
    assert last == [{"type": "done"}]
    final = await service.view(session_id)
    assert (final.session.status, final.session.current_topic_id) == ("finished", None)
    assert final.waiting is None
    assert [(t.ordinal, t.speaker) for t in final.turns] == [
        (1, "interviewer"),
        (2, "expert"),
        (3, "interviewer"),
        (4, "expert"),
        (5, "interviewer"),
        (6, "expert"),
    ]
    assert {n.topic_id for n in final.notes} == set(TOPIC_IDS)
    assert len(final.notes) == 21
    with pytest.raises(ConflictError, match="finished"):
        await service.post(session_id, text="One more thing.")


async def test_session_resumes_after_restart(scratch: Scratch, run_id: UUID) -> None:
    first_process = make_service(scratch.dsn)
    plan = await first_process.draft_plan(run_id)
    view = await first_process.approve(plan.id, "Nadia Ferrand")
    session_id = view.session.id
    await say(first_process, session_id, "Answer to the first topic.")
    before = await first_process.view(session_id)

    second_process = make_service(scratch.dsn)
    after = await second_process.view(session_id)
    assert after == before, "nothing lost by the restart"
    assert after.waiting is not None
    assert (after.waiting.ordinal, after.waiting.topic_id) == (3, TOPIC_IDS[1])
    events = await say(second_process, session_id, "Answer to the second topic.")
    assert events[-1]["type"] == "question"
    assert events[-1]["topic_id"] == TOPIC_IDS[2]
    turns = (await second_process.view(session_id)).turns
    assert [t.ordinal for t in turns] == [1, 2, 3, 4, 5], "no turn lost or repeated"
    assert [t.text for t in turns if t.speaker == "expert"] == [
        "Answer to the first topic.",
        "Answer to the second topic.",
    ]


async def test_stopped_step_is_continued_without_using_the_message(
    scratch: Scratch, run_id: UUID
) -> None:
    """The process died after approval was stored but before the first question was asked."""
    failing = {"on": True}

    async def flaky_agent(deps: Deps, state: InterviewState) -> dict[str, Any]:
        if failing["on"]:
            raise ModelError("down")
        return await one_question_agent(deps, state)

    service = make_service(scratch.dsn, agent=flaky_agent)
    plan = await service.draft_plan(run_id)
    with pytest.raises(ModelError):
        await service.approve(plan.id, "Nadia Ferrand")
    with scratch.connect() as conn:
        session_id = store.session_of_plan(conn, plan.id)
    assert session_id is not None
    failing["on"] = False
    events = await say(service, session_id, "Hello?")
    assert [e["type"] for e in events] == ["recovered", "question"]
    assert [t.speaker for t in (await service.view(session_id)).turns] == ["interviewer"]


async def test_incomplete_topic_goes_back_to_the_agent(scratch: Scratch, run_id: UUID) -> None:
    seen_notices: list[list[str]] = []

    async def lazy_agent(deps: Deps, state: InterviewState) -> dict[str, Any]:
        """Declares the topic done with nothing recorded, then fills it when told."""
        seen_notices.append(list(state.get("notices", [])))
        if not state.get("notices"):
            return {"topic_done": True}
        session_id, topic_id = UUID(str(state["session_id"])), str(state["topic_id"])
        for section in store.checklist(deps.conn, session_id, topic_id):
            store.mark_open(
                deps.conn, session_id, topic_id, section, RedactedText("not covered"), turn=None
            )
        return {"topic_done": True, "notices": []}

    service = make_service(scratch.dsn, agent=lazy_agent)
    plan = await service.draft_plan(run_id)
    view = await service.approve(plan.id, "Nadia Ferrand")
    assert view.session.status == "finished"
    assert len(view.open_items) == 21
    assert seen_notices[0] == []
    assert seen_notices[1][0].startswith(
        "The topic is not finished. These sections are still missing: trigger"
    )


async def test_edits_after_approval_and_double_approval_are_refused(
    scratch: Scratch, run_id: UUID
) -> None:
    service = make_service(scratch.dsn)
    plan = await service.draft_plan(run_id)
    await service.approve(plan.id, "Nadia Ferrand")
    with pytest.raises(PlanFrozenError):
        await service.edit_plan(plan.id, lambda p: remove_topic(p, TOPIC_IDS[0]))
    with pytest.raises(ConflictError, match="already approved"):
        await service.approve(plan.id, "Nadia Ferrand")


async def test_unknown_ids_and_unmapped_runs(scratch: Scratch) -> None:
    service = make_service(scratch.dsn)
    for call in (
        service.draft_plan(uuid4()),
        service.get_plan(uuid4()),
        service.edit_plan(uuid4(), lambda p: p),
        service.approve(uuid4(), "Nadia"),
        service.view(uuid4()),
        service.post(uuid4(), text="hi"),
    ):
        with pytest.raises(NotFoundError):
            await call
    with scratch.connect() as conn:
        unmapped = seed_run(conn, mapped=False)
    with pytest.raises(ConflictError, match="no gap scores"):
        await service.draft_plan(unmapped)


async def test_model_failure_while_drafting_leaves_no_plan(scratch: Scratch, run_id: UUID) -> None:
    service = make_service(scratch.dsn, drafter=ScriptedModel([ModelError("down")]))
    with pytest.raises(ModelError):
        await service.draft_plan(run_id)
    with scratch.connect() as conn:
        assert conn.execute("SELECT count(*) FROM interview_plans").fetchone() == (0,)
        assert conn.execute("SELECT count(*) FROM checkpoints").fetchone() == (0,)
    plan = await make_service(scratch.dsn).draft_plan(run_id)
    assert len(plan.topics) == 3


async def test_a_second_message_while_one_is_handled_is_refused(
    scratch: Scratch, run_id: UUID
) -> None:
    service = make_service(scratch.dsn)
    plan = await service.draft_plan(run_id)
    view = await service.approve(plan.id, "Nadia Ferrand")
    stream = await service.post(view.session.id, text="First.")
    with pytest.raises(BusyError):
        await service.post(view.session.id, text="Second.")
    assert [e["type"] async for e in stream][-1] == "question"
    assert (await say(service, view.session.id, "Now it is free."))[-1]["type"] == "question"


async def test_drafting_uses_no_tools(scratch: Scratch, run_id: UUID) -> None:
    drafter = ScriptedModel(quoting_drafter, name="drafter")
    await make_service(scratch.dsn, drafter=drafter).draft_plan(run_id)
    assert len(drafter.calls) == 3
    assert all(call.tools == () for call in drafter.calls)
