"""Phase 2 acceptance: a whole interview over the fixture corpus, with no real model.

The drafter and the interviewer are rule-following fakes and the expert is the
scripted one from the fixtures, so this proves the workflow, the storage, the
redaction and the resume. It does not prove that a real model asks well; that
is ``tests/test_live_model.py`` (opt-in) and the human-owned eval scenarios.
"""

from dataclasses import dataclass
from typing import Any
from uuid import UUID

import pytest

from eki.ingest.embedding import HashingEmbedder
from eki.ingest.index import index_run
from eki.ingest.models import Snapshot
from eki.ingest.pipeline import store as store_snapshot
from eki.interview.models import SECTIONS, InterviewPlan
from eki.interview.plan import remove_topic
from eki.interview.service import InterviewService, SessionView
from eki.interview.tools import TOOLS
from eki.mapping.store import save_mapping
from tests.conftest import Scratch
from tests.fakes import (
    RecordingTracer,
    ScriptedModel,
    SimulatedExpert,
    checklist_interviewer,
    expert_answers,
)
from tests.interview_support import make_service, quoting_drafter
from tests.support import tables_containing
from tests.test_phase1_acceptance import INJECTION, Result, run_pipeline

pytestmark = [pytest.mark.db, pytest.mark.anyio, pytest.mark.eval]
GAPS = ("G1", "G2", "G3", "G7")


@dataclass
class World:
    scratch: Scratch
    result: Result
    run_id: UUID
    gap_of_topic: dict[str, str]
    drafter: ScriptedModel
    interviewer: ScriptedModel
    tracer: RecordingTracer

    def service(self) -> InterviewService:
        """A fresh service, as after a process restart. The fakes keep recording."""
        return make_service(
            self.scratch.dsn,
            agent=None,
            drafter=self.drafter,
            interviewer=self.interviewer,
            tracer=self.tracer,
        )

    def topic_of(self, gap: str) -> str:
        return next(topic for topic, name in self.gap_of_topic.items() if name == gap)


@pytest.fixture
async def world(scratch: Scratch, snapshot: Snapshot) -> World:
    result = await run_pipeline(snapshot)
    with scratch.connect() as conn:
        run_id = store_snapshot(conn, snapshot)
        save_mapping(conn, run_id, result.topics, result.scores)
        index_run(conn, run_id, HashingEmbedder())
    gap_of_topic = {}
    for gap in GAPS:
        topic = result.topic_for(gap)
        assert topic is not None, gap
        gap_of_topic[topic.id] = gap
    return World(
        scratch=scratch,
        result=result,
        run_id=run_id,
        gap_of_topic=gap_of_topic,
        drafter=ScriptedModel(quoting_drafter, name="drafter"),
        interviewer=ScriptedModel(checklist_interviewer, name="interviewer"),
        tracer=RecordingTracer(),
    )


async def reviewed_plan(world: World) -> tuple[InterviewPlan, str]:
    """Draft the plan, then do what the expert would: drop one topic that is not a real gap."""
    service = world.service()
    plan = await service.draft_plan(world.run_id)
    filler = next(t.topic_id for t in plan.topics if t.topic_id not in world.gap_of_topic)
    return await service.edit_plan(plan.id, lambda p: remove_topic(p, filler)), filler


async def interview(world: World, plan: InterviewPlan, *, restart_every_step: bool) -> SessionView:
    """Answer every question as the simulated expert until the interview is done."""
    service = world.service()
    view = await service.approve(plan.id, "Nadia Ferrand")
    experts: dict[str, SimulatedExpert] = {}
    answers = expert_answers()
    for _ in range(200):
        if view.waiting is None:
            return view
        topic_id = str(view.waiting.topic_id)
        gap = world.gap_of_topic.get(topic_id)
        expert = experts.setdefault(topic_id, SimulatedExpert(answers[gap] if gap else None))
        if restart_every_step:
            service = world.service()
        events = [
            e
            async for e in await service.post(
                view.session.id, text=expert.answer(view.waiting.section)
            )
        ]
        assert events[-1]["type"] in ("question", "done"), events
        view = await service.view(view.session.id)
    raise AssertionError("the interview did not end")


def summary(view: SessionView) -> dict[str, Any]:
    """What the interview produced, without IDs that differ between two runs."""
    return {
        "turns": [(t.ordinal, t.topic_id, t.kind, t.text) for t in view.turns],
        "notes": sorted((n.topic_id, n.section, n.text, n.turn) for n in view.notes),
        "open": sorted((o.topic_id, o.section, o.reason, o.turn) for o in view.open_items),
    }


async def test_the_plan_comes_from_the_real_ranking(world: World) -> None:
    plan, filler = await reviewed_plan(world)
    ranked = [s.topic_id for s in world.result.scores]
    planned = [t.topic_id for t in plan.topics]
    assert filler in ranked[:10]
    assert filler not in planned
    top = [topic_id for topic_id in ranked[:10] if topic_id != filler]
    assert planned[: len(top)] == top, "ranked topics keep their order"
    assert set(world.gap_of_topic) <= set(planned), "the strongest planted gaps are all in the plan"
    assert plan.status == "draft"
    artifacts = {a.id: a for a in world.result.snapshot.artifacts}
    for topic in plan.topics:
        assert topic.questions, topic.topic_id
        for question in topic.questions:
            assert question.artifact_id in topic.artifact_ids
            assert question.quote
            source = artifacts[str(question.artifact_id)]
            assert " ".join(question.quote.split()) in " ".join(
                f"{source.title} {source.body}".split()
            )


async def test_contradicting_tickets_become_a_question_that_does_not_pick(world: World) -> None:
    """T6: two tickets disagree on the restart order. The plan asks; it does not choose."""
    plan, _ = await reviewed_plan(world)
    topic = plan.topic(world.topic_of("G1"))
    assert topic is not None
    first = topic.questions[0]
    conflict = set(world.result.key["traps"]["T6"]["artifact_ids"])
    assert {first.artifact_id} < conflict
    assert all(artifact_id in first.text for artifact_id in conflict)
    assert "replicas first" in first.text
    assert "workers first" in first.text
    assert first.text.endswith("Which is right?")
    view = await interview(world, plan, restart_every_step=False)
    asked = [t.text for t in view.turns if t.topic_id == topic.topic_id and t.kind == "question"]
    assert asked[0] == first.text, "the interviewer put the contradiction to the expert"


async def test_g3_is_complete_after_one_follow_up(world: World) -> None:
    plan, _ = await reviewed_plan(world)
    view = await interview(world, plan, restart_every_step=False)
    topic_id = world.topic_of("G3")
    questions = [t for t in view.turns if t.topic_id == topic_id and t.kind == "question"]
    assert [q.section for q in questions] == ["trigger", "rollback"], "one follow-up, on rollback"
    notes = {n.section: n for n in view.notes if n.topic_id == topic_id}
    assert set(notes) == set(SECTIONS)
    assert notes["rollback"].text == expert_answers()["G3"]["follow_ups"]["rollback"]
    assert notes["rollback"].turn == questions[1].ordinal + 1
    assert not [o for o in view.open_items if o.topic_id == topic_id]


async def test_g7_ends_open_in_her_words(world: World) -> None:
    plan, _ = await reviewed_plan(world)
    view = await interview(world, plan, restart_every_step=False)
    topic_id = world.topic_of("G7")
    assert not [n for n in view.notes if n.topic_id == topic_id], "nothing was invented"
    open_items = [o for o in view.open_items if o.topic_id == topic_id]
    assert sorted(o.section for o in open_items) == sorted(SECTIONS)
    assert {o.reason for o in open_items} == {'The expert said: "I don\'t remember"'}
    answer = next(t for t in view.turns if t.topic_id == topic_id and t.kind == "answer")
    assert answer.text == "I don't remember"
    assert {o.turn for o in open_items} == {answer.ordinal}


async def test_the_interview_runs_to_the_end(world: World) -> None:
    plan, _ = await reviewed_plan(world)
    view = await interview(world, plan, restart_every_step=False)
    assert view.session.status == "finished"
    covered = {(n.topic_id, n.section) for n in view.notes} | {
        (o.topic_id, o.section) for o in view.open_items
    }
    assert covered == {(t.topic_id, s) for t in plan.topics for s in SECTIONS}
    assert all(call.tools == TOOLS for call in world.interviewer.calls)
    assert all(call.tools == () for call in world.drafter.calls)


async def test_nothing_stored_sent_or_traced_holds_a_planted_secret(world: World) -> None:
    plan, _ = await reviewed_plan(world)
    await interview(world, plan, restart_every_step=False)
    secrets: list[str] = world.result.key["never_index"]
    assert len(secrets) == 5
    assert world.tracer.calls, "the interview was traced"
    with world.scratch.connect() as conn:
        for secret in secrets:
            assert tables_containing(conn, world.scratch.schema, secret) == [], secret
            assert secret not in world.drafter.sent_text()
            assert secret not in world.interviewer.sent_text()
            assert secret not in world.tracer.text()


async def test_the_planted_instruction_stays_data(world: World) -> None:
    """T1: the instruction in a ticket comment reaches a model only inside data delimiters."""
    plan, _ = await reviewed_plan(world)
    view = await interview(world, plan, restart_every_step=False)
    for model in (world.drafter, world.interviewer):
        for call in model.calls:
            assert INJECTION not in call.messages[0].content, "never in a system message"
            for message in call.messages[1:]:
                if INJECTION in message.content:
                    before = message.content[: message.content.index(INJECTION)]
                    assert before.count("<<<") > before.count("END>>>"), "inside a data block"
    assert (await world.service().get_plan(plan.id)).approved_by == "Nadia Ferrand"
    assert view.session.status == "finished"
    with world.scratch.connect() as conn:
        assert conn.execute("SELECT count(*) FROM interview_plans").fetchone() == (1,)


async def test_restarting_after_every_message_changes_nothing(world: World) -> None:
    """Kill and reopen the session at every step: the final state is the same."""
    plan, _ = await reviewed_plan(world)
    straight = await interview(world, plan, restart_every_step=False)
    other, _ = await reviewed_plan(world)
    restarted = await interview(world, other, restart_every_step=True)
    assert summary(restarted) == summary(straight)
    assert len(straight.turns) > 20
