"""P3b: actual fixture interview -> approvals -> useful, isolated retrieval and export."""

from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from eki import access
from eki.api.app import create_app
from eki.runbook.service import RunbookService
from eki.search.service import SearchService
from tests.fakes import SimulatedExpert, expert_answers
from tests.search_support import ADMIN, OWNER
from tests.support import FAKE_CREDENTIALS
from tests.test_api_access import authenticator, headers
from tests.test_phase2_acceptance import World, reviewed_plan
from tests.test_phase2_acceptance import world as world

pytestmark = [pytest.mark.db, pytest.mark.anyio, pytest.mark.eval]


async def test_real_fixture_search_answers_and_exports(world: World) -> None:
    plan, _ = await reviewed_plan(world)
    service = world.service()
    view = await service.approve(plan.id, OWNER.name)
    experts: dict[str, SimulatedExpert] = {}
    answers = expert_answers()
    private_text = "Confidential handover detail that must never reach retrieval"
    confidential_topic = None
    for _ in range(200):
        if view.waiting is None:
            break
        topic = str(view.waiting.topic_id)
        gap = world.gap_of_topic.get(topic)
        expert = experts.setdefault(topic, SimulatedExpert(answers[gap] if gap else None))
        private = gap is None and confidential_topic is None
        text = (
            f"{private_text} password={FAKE_CREDENTIALS[1]}"
            if private
            else expert.answer(view.waiting.section)
        )
        if private:
            confidential_topic = topic
        events = [
            e async for e in await service.post(view.session.id, text=text, confidential=private)
        ]
        assert events[-1]["type"] in ("question", "done")
        service = world.service()
        view = await service.view(view.session.id)
    assert view.session.status == "finished" and confidential_topic is not None
    assert private_text not in str(world.interviewer.calls)
    books = RunbookService(world.scratch.dsn)
    entries = {e.entry.topic_id: e.entry for e in books.list(view.session.id)}
    for gap in ("G1", "G2", "G3", "G7"):
        books.approve(UUID(entries[world.topic_of(gap)].id), OWNER.name)
    books.approve(UUID(entries[confidential_topic].id), OWNER.name)
    with world.scratch.connect() as conn:
        access.set_owner(conn, world.run_id, OWNER.subject, ADMIN)
        access.set_readers(conn, world.run_id, ["reader"], OWNER)
    client = TestClient(
        create_app(
            runbook_service=books,
            search_service=SearchService(world.scratch.dsn),
            authenticator=authenticator(),
        )
    )
    # Human-readable questions with expected topics/sections, independent of rank scores.
    cases = (
        (
            "month-end reconciliation",
            "G3",
            "How do I roll back month-end reconciliation?",
            "rollback",
            "pre-run export",
        ),
        (
            "ledger replicas workers",
            "G1",
            "How do I restart ledger replicas and workers?",
            "steps",
            "",
        ),
        (
            "report export workbook",
            "G2",
            "Why is report-export knowledge concentrated?",
            "rationale",
            "Nobody else picked it up",
        ),
    )
    for query, gap, question, section, phrase in cases:
        response = client.post("/runbook/search", json={"query": query}, headers=headers("reader"))
        assert response.status_code == 200
        hits = response.json()["results"]
        expected = entries[world.topic_of(gap)].id
        assert hits and hits[0]["id"] == expected, (query, hits)
        result = client.post("/runbook/ask", json={"question": question}, headers=headers("reader"))
        assert result.status_code == 200
        answer = result.json()
        assert answer["status"] == "answered", (question, answer)
        assert answer["evidence"] and all(
            e["entry_id"] == expected and e["section"] == section for e in answer["evidence"]
        ), (question, answer)
        assert phrase in answer["answer"]
        assert all(e["citations"] for e in answer["evidence"])
    export = client.get(
        "/runbook/export", params={"run_id": str(world.run_id)}, headers=headers("reader")
    )
    assert export.status_code == 200
    assert "Open question" in export.text and "turn " in export.text
    assert private_text not in export.text
    assert all(secret not in export.text for secret in FAKE_CREDENTIALS)
    with world.scratch.connect() as conn:
        indexed = conn.execute("SELECT body FROM runbook_index").fetchall()
        assert private_text not in str(indexed)
        assert all(secret not in str(indexed) for secret in FAKE_CREDENTIALS)
    assert (
        client.post(
            "/runbook/search",
            json={"query": "month-end reconciliation"},
            headers=headers("stranger"),
        ).json()["results"]
        == []
    )
    assert (
        client.get(
            "/runbook/export", params={"run_id": str(world.run_id)}, headers=headers("stranger")
        ).status_code
        == 404
    )
    with world.scratch.connect() as conn:
        access.set_readers(conn, world.run_id, [], OWNER)
    assert (
        client.post(
            "/runbook/search", json={"query": "month-end reconciliation"}, headers=headers("reader")
        ).json()["results"]
        == []
    )
    assert (
        client.get(
            "/runbook/export", params={"run_id": str(world.run_id)}, headers=headers("reader")
        ).status_code
        == 404
    )
