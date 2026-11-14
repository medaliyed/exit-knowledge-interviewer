"""F16: the browser shell and its navigation endpoints preserve API access rules."""

import re
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from eki.access import set_owner, set_readers
from eki.api.app import create_app
from eki.runbook.service import RunbookService
from eki.web import routes
from tests.conftest import Scratch
from tests.interview_support import make_service
from tests.test_api_access import ADMIN, OWNER, authenticator, headers
from tests.test_workflow_synthesis import finished


def test_browser_paths_do_not_intercept_api_or_missing_assets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    build = tmp_path / "workspace"
    build.mkdir()
    shell = '<div id="root"></div><script src="/assets/workspace/app.js"></script>'
    (build / "index.html").write_text(shell)
    (build / "app.js").write_text("console.log('workspace');")
    (tmp_path / "app.js").write_text("console.log('legacy client');")
    monkeypatch.setattr(routes, "ASSETS", tmp_path)
    client = TestClient(create_app())
    for path in ("/", "/workspace", "/workspace/", "/workspace/handovers/example/plans"):
        response = client.get(path)
        assert response.status_code == 200
        assert response.text == shell
        assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
        assert "script-src 'self'" in response.headers["content-security-policy"]
        assert "style-src 'self'" in response.headers["content-security-policy"]
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-content-type-options"] == "nosniff"
    assert client.get("/assets/workspace/app.js").text == "console.log('workspace');"
    assert client.get("/assets/app.js").text == "console.log('legacy client');"
    assert client.get("/assets/../config.py").status_code == 404
    assert client.get("/assets/workspace/missing.js").status_code == 404
    assert client.get("/workspace/missing.js").status_code == 404
    assert client.get("/not-a-workspace-route").status_code == 404
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/runs").status_code == 401
    assert client.get("/plans/00000000-0000-0000-0000-000000000000").status_code == 401
    assert client.post("/workspace/handovers/example/plans").status_code == 405


def test_packaged_workspace_build_serves_local_javascript_and_styles() -> None:
    client = TestClient(create_app())
    response = client.get("/")
    assert response.status_code == 200
    assert '<div id="root"></div>' in response.text
    assets = re.findall(r'(?:src|href)="([^"]+)"', response.text)
    assert any(asset.endswith(".js") for asset in assets)
    assert any(asset.endswith(".css") for asset in assets)
    for asset in assets:
        assert asset.startswith("/assets/workspace/")
        resource = client.get(asset)
        assert resource.status_code == 200
        assert "text/html" not in resource.headers["content-type"]
    assert client.get("/workspace/example/plans").text == response.text


@pytest.mark.db
@pytest.mark.parametrize("subject", ["owner", "admin"])
def test_plan_approval_requires_the_authenticated_expert(scratch: Scratch, subject: str) -> None:
    from eki.interview import store
    from tests.interview_support import seed_run

    with scratch.connect() as conn:
        run = seed_run(conn)
        set_owner(conn, run, OWNER.subject, ADMIN)
        if subject == "owner":
            conn.execute("UPDATE ingest_runs SET expert_name='Another Expert' WHERE id=%s", (run,))
    service = make_service(scratch.dsn)
    client = TestClient(create_app(service, authenticator=authenticator()))
    plan = client.post(f"/runs/{run}/plan", headers=headers(subject)).json()
    name = OWNER.name if subject == "owner" else "admin"
    response = client.post(
        f"/plans/{plan['id']}/approve",
        headers=headers(subject),
        json={"approved_by": name},
    )
    assert response.status_code == 422
    assert response.json()["detail"] == "reviewer must match the run's expert"
    assert client.get(f"/plans/{plan['id']}", headers=headers(subject)).json()["status"] == "draft"
    with scratch.connect() as conn:
        assert store.session_of_plan(conn, UUID(plan["id"])) is None


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
