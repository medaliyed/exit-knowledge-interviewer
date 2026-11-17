"""Opt-in check of a REAL model: ``uv run pytest -m live -s``.

Needs ``EKI_MODEL_INTERVIEWER``, ``EKI_MODEL_DRAFTER`` and the provider key (for
example ``GROQ_API_KEY``) in the environment, plus Postgres. It costs
money and is never run in CI. The simulated expert answers for three planted
gaps only, and the test checks the three behaviours a scripted fake cannot
show: a follow-up on G3's missing rollback, G7 left open instead of invented,
and the G1 contradiction put to the expert.
"""

from uuid import UUID

import pytest

from eki.config import ConfigError, get_settings, model_name, work_domains
from eki.ingest.embedding import HashingEmbedder
from eki.ingest.index import index_run
from eki.ingest.models import Snapshot
from eki.ingest.pipeline import store as store_snapshot
from eki.interview.models import SECTIONS
from eki.interview.service import InterviewService
from eki.llm.litellm_model import LiteLLMModel
from eki.mapping.store import save_mapping
from tests.conftest import Scratch
from tests.fakes import RecordingTracer, SimulatedExpert, expert_answers
from tests.live_support import has_provider_key
from tests.test_phase1_acceptance import run_pipeline

pytestmark = [pytest.mark.live, pytest.mark.anyio]
GAPS = ("G1", "G3", "G7")


def real_models() -> tuple[LiteLLMModel, LiteLLMModel]:
    settings = get_settings()
    try:
        names = model_name(settings, "drafter"), model_name(settings, "interviewer")
    except ConfigError as missing:
        pytest.skip(str(missing))
    if not has_provider_key():
        pytest.skip("no provider key in the environment")
    domains = work_domains(settings)
    timeout = settings.model_timeout_seconds
    drafter, interviewer = (
        LiteLLMModel(name, timeout=timeout, work_email_domains=domains) for name in names
    )
    return drafter, interviewer


async def test_real_model_follows_up_leaves_open_and_asks_about_the_contradiction(
    scratch: Scratch, snapshot: Snapshot
) -> None:
    drafter, interviewer = real_models()
    result = await run_pipeline(snapshot)
    topics = {gap: result.topic_for(gap) for gap in GAPS}
    assert all(topics.values())
    chosen = tuple(t for t in topics.values() if t is not None)
    gap_of_topic = {t.id: gap for gap, t in topics.items() if t is not None}
    with scratch.connect() as conn:
        run_id: UUID = store_snapshot(conn, snapshot)
        scores = tuple(s for s in result.scores if s.topic_id in gap_of_topic)
        save_mapping(conn, run_id, chosen, scores)
        index_run(conn, run_id, HashingEmbedder())
    tracer = RecordingTracer()
    service = InterviewService(
        dsn=scratch.dsn,
        drafter=drafter,
        interviewer=interviewer,
        embedder=HashingEmbedder(),
        work_email_domains=work_domains(get_settings()),
        tracer=tracer,
    )
    plan = await service.draft_plan(run_id)
    view = await service.approve(plan.id, "Nadia Ferrand", expected_revision=plan.revision)
    experts: dict[str, SimulatedExpert] = {}
    answers = expert_answers()
    for _ in range(60):
        if view.waiting is None:
            break
        topic_id = str(view.waiting.topic_id)
        expert = experts.setdefault(topic_id, SimulatedExpert(answers[gap_of_topic[topic_id]]))
        answer = expert.answer(view.waiting.section)
        events = [e async for e in await service.post(view.session.id, text=answer)]
        assert events[-1]["type"] != "error", events
        view = await service.view(view.session.id)

    for turn in view.turns:
        print(f"[{turn.ordinal}] {turn.speaker} ({turn.topic_id}): {turn.text}")
    spent = sum(c.input_tokens for c in tracer.calls), sum(c.output_tokens for c in tracer.calls)
    print(f"model calls: {len(tracer.calls)}, input tokens: {spent[0]}, output tokens: {spent[1]}")

    assert view.session.status == "finished"
    by_gap = {gap: topic_id for topic_id, gap in gap_of_topic.items()}
    g3_questions = [t for t in view.turns if t.topic_id == by_gap["G3"] and t.kind == "question"]
    assert any(q.section == "rollback" for q in g3_questions[1:]), "no follow-up on G3's rollback"
    assert any(n.topic_id == by_gap["G3"] and n.section == "rollback" for n in view.notes)
    assert not [n for n in view.notes if n.topic_id == by_gap["G7"]], (
        "G7 was filled in, not left open"
    )
    g7_open = {o.section for o in view.open_items if o.topic_id == by_gap["G7"]}
    assert g7_open == set(SECTIONS)
    g1_questions = [
        t.text for t in view.turns if t.topic_id == by_gap["G1"] and t.kind == "question"
    ]
    conflict = result.key["traps"]["T6"]["artifact_ids"]
    assert any(all(i in q for i in conflict) for q in g1_questions), (
        "the contradiction was not asked"
    )
