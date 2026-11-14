"""P4-e: consented mail -> interview -> explicit review -> safe retrieval/export + audit."""

import json
from dataclasses import replace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from eki import access
from eki.api.app import create_app
from eki.audit import AuditRecorder, audit_context
from eki.ingest.embedding import HashingEmbedder
from eki.ingest.index import index_run
from eki.ingest.pipeline import ingest_from_source, store
from eki.mapping.models import TopicKind
from eki.mapping.scoring import score_topics
from eki.mapping.store import save_mapping
from eki.mapping.topics import build_topics
from eki.runbook.models import SECTION_NAMES
from eki.runbook.service import RunbookService
from eki.search.service import SearchService
from tests.conftest import Scratch
from tests.fakes import (
    RecordingTracer,
    ScriptedModel,
    SimulatedExpert,
    checklist_interviewer,
    expert_answers,
)
from tests.interview_support import make_service, quoting_drafter, say
from tests.search_support import ADMIN, OWNER
from tests.support import CORPUS, FAKE_CREDENTIALS, FIXTURES, FULL_SPAN
from tests.test_api_access import authenticator, headers

pytestmark = [pytest.mark.db, pytest.mark.anyio, pytest.mark.eval, pytest.mark.redteam]


async def test_selected_mail_to_approved_runbook_and_audited_access(scratch: Scratch) -> None:
    run_id = uuid4()
    scope = replace(FULL_SPAN, mail_folders=("Inbox", "Sent", "Vendors"))
    key = json.loads((FIXTURES / "answer_key.json").read_text())
    private_note = "PRIVATE HANDOVER CANARY THAT MUST STAY OUT OF RETRIEVAL"
    with audit_context(
        user_id=OWNER.subject, run_ids=(run_id,), recorder=AuditRecorder(scratch.dsn)
    ):
        snapshot = await ingest_from_source(scope, CORPUS)
        assert not set(key["excluded_mail_ids"]) & {a.id for a in snapshot.artifacts}
        topics = build_topics(snapshot, HashingEmbedder())
        wanted = set(key["gaps"]["G8"]["artifact_ids"])
        topic = next(
            t for t in topics if t.kind == TopicKind.DECISION and wanted <= set(t.artifact_ids)
        )
        score = next(
            s
            for s in score_topics(topics, snapshot, as_of=snapshot.until)
            if s.topic_id == topic.id
        )
        with scratch.connect() as conn:
            store(conn, snapshot, run_id=run_id)
            index_run(conn, run_id, HashingEmbedder())
            save_mapping(conn, run_id, (topic,), (score,))
            access.set_owner(conn, run_id, OWNER.subject, ADMIN)
            access.set_readers(conn, run_id, ["reader"], OWNER)
        model = ScriptedModel(checklist_interviewer)
        tracer = RecordingTracer()
        service = make_service(
            scratch.dsn,
            agent=None,
            drafter=ScriptedModel(quoting_drafter),
            interviewer=model,
            tracer=tracer,
        )
        plan = await service.draft_plan(run_id)
        view = await service.approve(plan.id, OWNER.name)
        # Confidential content is retained privately, but never becomes model/audit/runbook data.
        await say(
            service,
            view.session.id,
            private_note + " password=" + FAKE_CREDENTIALS[1],
            confidential=True,
        )
        view = await service.view(view.session.id)
        expert = SimulatedExpert(expert_answers()["G8"])
        for _ in range(20):
            if view.waiting is None:
                break
            await say(service, view.session.id, expert.answer(view.waiting.section))
            view = await service.view(view.session.id)
        assert view.session.status == "finished"
        assert private_note not in model.sent_text() and private_note not in tracer.text()
    books = RunbookService(scratch.dsn)
    entry = books.list(view.session.id)[0].entry
    assert entry.status == "draft" and not entry.missing_sections()
    assert entry.trigger.open_question is not None
    assert "Answered confidentially" in entry.trigger.open_question
    client = TestClient(
        create_app(
            service,
            runbook_service=books,
            search_service=SearchService(scratch.dsn),
            authenticator=authenticator(),
        )
    )
    assert client.get(f"/entries/{entry.id}", headers=headers("reader")).status_code == 404
    assert (
        client.post(
            f"/entries/{entry.id}/approve",
            headers=headers("reader"),
            json={"approved_by": OWNER.name},
        ).status_code
        == 404
    )
    approved = client.post(
        f"/entries/{entry.id}/approve", headers=headers("owner"), json={"approved_by": OWNER.name}
    )
    assert approved.status_code == 200
    search = client.get(
        "/runbook/search", headers=headers("reader"), params={"q": "ledger-db backup"}
    )
    assert search.status_code == 200 and search.json()["results"][0]["id"] == entry.id
    answer = client.post(
        "/runbook/ask",
        headers=headers("reader"),
        json={"question": "Why is ledger-db backup different?"},
    )
    assert answer.status_code == 200 and answer.json()["status"] == "answered", answer.text
    assert all(e["section"] == "rationale" and e["citations"] for e in answer.json()["evidence"])
    exported = client.get(f"/entries/{entry.id}/markdown", headers=headers("reader"))
    assert exported.status_code == 200 and "turn " in exported.text
    for section in SECTION_NAMES:
        assert "## " + section.title() in exported.text
    log = client.get(
        f"/runs/{run_id}/audit", headers=headers("owner"), params={"limit": 500}
    ).json()["events"]
    assert log and {e["user_id"] for e in log} == {OWNER.subject}
    assert {e["kind"] for e in log} >= {"model", "tool", "approval", "access"}
    assert any(e["details"]["operation"] == "list_messages" for e in log)
    assert any(e["details"]["operation"] == "approve" for e in log)
    for text in (str(log), exported.text, answer.text, search.text):
        assert private_note not in text and all(secret not in text for secret in FAKE_CREDENTIALS)
    edited = client.patch(
        f"/entries/{entry.id}",
        headers=headers("owner"),
        json={
            "edited_by": OWNER.name,
            "sections": {"steps": {"claims": ["Review the backup schedule first."]}},
        },
    )
    assert edited.status_code == 200 and edited.json()["status"] == "draft"
    assert client.get(f"/entries/{entry.id}/markdown", headers=headers("reader")).status_code == 404
    assert (
        client.get(
            "/runbook/search", headers=headers("reader"), params={"q": "ledger-db backup"}
        ).json()["results"]
        == []
    )
    client.post(
        f"/entries/{entry.id}/approve", headers=headers("owner"), json={"approved_by": OWNER.name}
    )
    client.put(f"/runs/{run_id}/access", headers=headers("owner"), json={"readers": []})
    assert client.get(f"/entries/{entry.id}/markdown", headers=headers("reader")).status_code == 404
    assert client.get(f"/runs/{run_id}/audit", headers=headers("reader")).status_code == 404
