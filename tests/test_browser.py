"""F16: the browser shell and its navigation endpoints preserve API access rules."""

from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from eki.access import set_owner, set_readers
from eki.api.app import create_app
from eki.runbook.service import RunbookService
from tests.conftest import Scratch
from tests.interview_support import make_service
from tests.test_api_access import ADMIN, OWNER, authenticator, headers
from tests.test_workflow_synthesis import finished


def test_browser_shell_has_no_embedded_credentials() -> None:
    client = TestClient(create_app())
    response = client.get("/")
    assert response.status_code == 200
    assert 'id="login"' in response.text
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    script = client.get("/assets/app.js")
    assert script.status_code == 200
    assert "localStorage" not in script.text and "innerHTML" not in script.text
    assert client.get("/assets/../config.py").status_code == 404


@pytest.mark.db
@pytest.mark.anyio
async def test_navigation_lists_only_accessible_data(scratch: Scratch) -> None:
    sid = await finished(scratch)
    books = RunbookService(scratch.dsn)
    public = books.approve(UUID(books.list(sid)[0].entry.id), OWNER.name)
    service = make_service(scratch.dsn)
    view = await service.view(sid)
    run = view.session.run_id
    with scratch.connect() as conn:
        set_owner(conn, run, OWNER.subject, ADMIN)
        set_readers(conn, run, ["reader"], OWNER)
    client = TestClient(create_app(service, authenticator=authenticator()))
    assert client.get("/runs").status_code == 401
    assert client.get("/runbook/identity", headers=headers("owner")).json()["name"] == OWNER.name
    assert client.get("/runs", headers=headers("stranger")).json() == []
    assert client.get("/runs", headers=headers("owner")).json()[0]["can_manage"]
    assert not client.get("/runs", headers=headers("reader")).json()[0]["can_manage"]
    plans = client.get(f"/runs/{run}/plans", headers=headers("owner")).json()
    assert plans[0]["session_id"] == str(sid)
    assert client.get(f"/runs/{run}/plans", headers=headers("reader")).status_code == 404
    listed = client.get(f"/runbook/entries?run_id={run}", headers=headers("reader")).json()
    assert [e["id"] for e in listed] == [public.entry.id]
    assert client.get(f"/runbook/entries?run_id={run}", headers=headers("stranger")).json() == []
    for path in (f"/sessions/{sid}/messages", f"/sessions/{sid}/messages/{UUID(int=1)}"):
        assert client.get(path, headers=headers("reader")).status_code == 404
    assert client.post(f"/sessions/{sid}/resume", headers=headers("reader")).status_code == 404


@pytest.mark.db
def test_complete_browser_api_workflow(scratch: Scratch) -> None:
    from eki.interview import store
    from tests.fakes import ScriptedModel, checklist_interviewer
    from tests.interview_support import seed_run
    from tests.test_api_interview import events

    with scratch.connect() as conn:
        run = seed_run(conn)
        set_owner(conn, run, OWNER.subject, ADMIN)
    service = make_service(
        scratch.dsn, agent=None, interviewer=ScriptedModel(checklist_interviewer)
    )
    client = TestClient(
        create_app(service, authenticator=authenticator()), headers=headers("owner")
    )
    plan = client.post(f"/runs/{run}/plan").json()
    pid = plan["id"]
    edit = client.patch(
        f"/plans/{pid}", json={"remove": [t["topic_id"] for t in plan["topics"][1:]]}
    )
    assert edit.status_code == 200 and len(edit.json()["topics"]) == 1
    view = client.post(f"/plans/{pid}/approve", json={"approved_by": OWNER.name}).json()
    sid, step_turn = view["id"], None
    for _ in range(12):
        question = view["waiting"]
        if question is None:
            break
        section = question["section"]
        body = {
            "message_id": str(uuid4()),
            "question_turn": question["turn"],
            "text": "Stop the billing worker. Start the billing worker.",
        }
        if section == "diagnosis":
            body.update(text="Private synthetic detail.", confidential=True)
        if section == "rollback":
            body.update(action="skip")
        if section == "steps":
            step_turn = question["turn"] + 1
        assert events(client.post(f"/sessions/{sid}/messages", json=body))[-1]["type"] in (
            "question",
            "done",
        )
        view = client.get(f"/sessions/{sid}").json()
    assert view["status"] == "finished"
    assert "Private synthetic detail." not in str(view)
    entries = client.get(f"/sessions/{sid}/entries").json()
    eid = entries[0]["id"]
    assert (
        client.post(f"/entries/{eid}/approve", json={"approved_by": OWNER.name}).status_code == 200
    )
    assert step_turn is not None
    events(
        client.post(
            f"/sessions/{sid}/messages",
            json={
                "message_id": str(uuid4()),
                "action": "correct",
                "corrects": step_turn,
                "text": (
                    "Stop the billing worker. Check logs, e.g. ledger.log. "
                    "Start the billing worker."
                ),
            },
        )
    )
    corrected = client.get(f"/entries/{eid}").json()
    assert corrected["status"] == "draft" and len(corrected["steps"]["claims"]) == 3
    assert (
        client.post(f"/entries/{eid}/approve", json={"approved_by": OWNER.name}).status_code == 200
    )
    search = client.post("/runbook/search", json={"query": "billing worker"}).json()
    assert search["results"][0]["id"] == eid
    answer = client.post(
        "/runbook/ask", json={"question": "How do I stop the billing worker?"}
    ).json()
    assert answer["status"] == "answered" and answer["evidence"]
    document = client.get(f"/entries/{eid}/markdown")
    assert document.status_code == 200 and "ledger" in document.text
    assert "Private synthetic detail." not in document.text
    with scratch.connect() as conn:
        transcript = store.load_turns(conn, UUID(sid))
        assert any(t.confidential for t in transcript)
