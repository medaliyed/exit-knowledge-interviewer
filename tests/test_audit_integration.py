"""F13: audited execution, actor binding, privacy and failure atomicity."""

from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from eki.audit import AuditedModel, AuditError, AuditRecorder, audit_context, list_events
from eki.ingest.redaction import RedactedText
from eki.interview import store as interviews
from eki.llm import Message, ModelError, Reply
from eki.runbook.service import RunbookService
from tests.api_support import create_app
from tests.conftest import Scratch
from tests.fakes import ScriptedModel, tool_reply
from tests.interview_support import make_service, seed_run
from tests.support import FAKE_CREDENTIALS
from tests.test_interviewer import ask
from tests.test_workflow_synthesis import finished

pytestmark = [pytest.mark.db, pytest.mark.anyio]


async def test_streamed_calls_carry_authenticated_identity_without_content(
    scratch: Scratch,
) -> None:
    with scratch.connect() as conn:
        run = seed_run(conn)
    service = make_service(
        scratch.dsn,
        agent=None,
        interviewer=ScriptedModel([tool_reply(ask("Trigger?")), ModelError("private error")]),
    )
    client = TestClient(create_app(service))
    plan = client.post(f"/runs/{run}/plan").json()
    session = client.post(f"/plans/{plan['id']}/approve", json={"approved_by": "Nadia Ferrand"})
    assert session.status_code == 200, session.text
    sid = session.json()["id"]
    client.post(
        f"/sessions/{sid}/messages",
        json={"text": f"private answer password={FAKE_CREDENTIALS[1]}", "confidential": True},
    )
    events = client.get(f"/runs/{run}/audit").json()["events"]
    assert events and {e["user_id"] for e in events} == {"test-owner"}
    assert {e["kind"] for e in events} >= {"model", "tool", "approval"}
    assert "private answer" not in str(events) and FAKE_CREDENTIALS[1] not in str(events)
    assert "private error" not in str(events)
    assert any(e["kind"] == "model" and e["phase"] == "failed" for e in events)
    calls = {e["call_id"] for e in events if e["phase"] == "started"}
    assert all(
        any(e["call_id"] == call and e["phase"] != "started" for e in events) for call in calls
    )


async def test_failed_audit_prevents_provider_call(
    scratch: Scratch, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = ScriptedModel([Reply(text="must not run")])

    def refuse(*args: object, **kwargs: object) -> None:
        raise AuditError("audit unavailable")

    recorder = AuditRecorder(scratch.dsn)
    monkeypatch.setattr(recorder, "record", refuse)
    audited = AuditedModel(model, recorder, "interview", ())
    with pytest.raises(AuditError):
        await audited.complete([Message("user", RedactedText("not logged"))])
    assert model.calls == []


async def test_failed_audit_rolls_back_entry_approval(
    scratch: Scratch, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = await finished(scratch)
    books = RunbookService(scratch.dsn)
    entry = books.list(session)[0]

    def refuse(*args: object, **kwargs: object) -> None:
        raise AuditError("unavailable")

    monkeypatch.setattr("eki.runbook.store.append_event", refuse)
    with pytest.raises(AuditError):
        books.approve(UUID(entry.entry.id), "Nadia Ferrand")
    assert books.get(UUID(entry.entry.id)) == entry
    assert books.history(UUID(entry.entry.id)) == ()


async def test_readers_cannot_read_audit_history(scratch: Scratch) -> None:
    from eki import access
    from eki.api.app import create_app as secured_app
    from tests.search_support import OWNER, search_world
    from tests.test_api_access import authenticator, headers

    world = await search_world(scratch)
    client = TestClient(
        secured_app(runbook_service=RunbookService(scratch.dsn), authenticator=authenticator())
    )
    url = f"/runs/{world.run_id}/audit"
    assert client.get(url, headers=headers("reader")).status_code == 404
    response = client.get(url, headers=headers("owner"), params={"limit": 1})
    assert response.status_code == 200 and len(response.json()["events"]) == 1
    assert client.get(url, headers=headers("owner"), params={"limit": 501}).status_code == 422
    with scratch.connect() as conn, audit_context(user_id=OWNER.subject):
        access.set_readers(conn, world.run_id, [], OWNER)
        events = list_events(conn, world.run_id)
        assert events[-1]["user_id"] == OWNER.subject and events[-1]["kind"] == "access"
        assert interviews.load_session(conn, world.session_id) is not None
