"""P3-h: fixture interview -> private cited drafts -> explicit expert decisions."""

from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from eki.runbook.models import SECTION_NAMES, InterviewTurnCitation
from eki.runbook.service import RunbookService
from tests.api_support import create_app
from tests.fakes import SimulatedExpert, expert_answers
from tests.support import FAKE_CREDENTIALS
from tests.test_phase2_acceptance import World, reviewed_plan
from tests.test_phase2_acceptance import world as world

pytestmark = [pytest.mark.db, pytest.mark.anyio, pytest.mark.eval]


async def test_fixture_interview_becomes_reviewable_runbook(world: World) -> None:
    service = world.service()
    plan, _ = await reviewed_plan(world)
    view = await service.approve(plan.id, "Nadia Ferrand")
    answers = expert_answers()
    experts: dict[str, SimulatedExpert] = {}
    confidential_turn: int | None = None
    private_text = "Private handover detail, never publish this."
    for _ in range(200):
        if view.waiting is None:
            break
        topic = str(view.waiting.topic_id)
        gap = world.gap_of_topic.get(topic)
        expert = experts.setdefault(topic, SimulatedExpert(answers[gap] if gap else None))
        private = gap is None and confidential_turn is None
        text = private_text if private else expert.answer(view.waiting.section)
        if private:
            confidential_turn = view.waiting.ordinal + 1
        events = [
            e async for e in await service.post(view.session.id, text=text, confidential=private)
        ]
        assert events[-1]["type"] in ("question", "done")
        service = world.service()
        view = await service.view(view.session.id)
    assert view.session.status == "finished"
    assert confidential_turn is not None
    books = RunbookService(world.scratch.dsn)
    entries = books.list(view.session.id)
    assert len(entries) == len(plan.topics)
    assert all(e.entry.status == "draft" for e in entries)
    by_topic = {e.entry.topic_id: e.entry for e in entries}
    g3 = by_topic[world.topic_of("G3")]
    assert all(g3.section(s).claims for s in SECTION_NAMES)
    rollback = g3.rollback.claims[0]
    assert rollback.text == answers["G3"]["follow_ups"]["rollback"]
    citation = rollback.citations[0]
    assert isinstance(citation, InterviewTurnCitation)
    question = next(t for t in view.turns if t.topic_id == g3.topic_id and t.section == "rollback")
    assert citation.turn_index == question.ordinal + 1
    g7 = by_topic[world.topic_of("G7")]
    assert all(not g7.section(s).claims for s in SECTION_NAMES)
    assert {g7.section(s).open_question for s in SECTION_NAMES} == {
        'The expert said: "I don\'t remember"'
    }
    turns = {t.ordinal: t for t in view.turns}
    client = TestClient(create_app(service))
    for stored in entries:
        entry = stored.entry
        assert not entry.missing_sections()
        for name in SECTION_NAMES:
            for claim in entry.section(name).claims:
                assert claim.citations
                for source in claim.citations:
                    assert isinstance(source, InterviewTurnCitation)
                    assert source.session_id == str(view.session.id)
                    turn = turns[source.turn_index]
                    assert turn.speaker == "expert" and not turn.confidential
                    assert turn.topic_id == entry.topic_id
                    assert source.turn_index != confidential_turn
        body = client.get(f"/entries/{entry.id}").text
        assert private_text not in body
        assert all(secret not in body for secret in FAKE_CREDENTIALS)
    assert any(
        "Answered confidentially; not published." in e.entry.model_dump_json() for e in entries
    )
    calls = (len(world.drafter.calls), len(world.interviewer.calls))
    books.approve(UUID(g3.id), "Nadia Ferrand")
    filler = next(e.entry for e in entries if e.entry.topic_id not in world.gap_of_topic)
    books.reject(UUID(filler.id), "Nadia Ferrand", "Not useful for the handover")
    final = books.synthesise(view.session.id)
    assert sum(e.entry.status == "approved" for e in final) == 1
    assert sum(e.entry.status == "rejected" for e in final) == 1
    assert sum(e.entry.status == "draft" for e in final) == len(entries) - 2
    assert calls == (len(world.drafter.calls), len(world.interviewer.calls))
