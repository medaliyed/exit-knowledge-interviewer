"""P3-f: HTTP review flow, useful status codes and safe validation errors."""

from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from eki.runbook.service import RunbookService
from tests.api_support import create_app
from tests.conftest import Scratch
from tests.interview_support import make_service, seed_run
from tests.support import FAKE_CREDENTIALS
from tests.test_workflow_synthesis import finished

pytestmark = [pytest.mark.db, pytest.mark.anyio]
EXPERT = "Nadia Ferrand"


async def test_api_review_flow(scratch: Scratch) -> None:
    session = await finished(scratch)
    client = TestClient(create_app(runbook_service=RunbookService(scratch.dsn)))
    listing = client.get(f"/sessions/{session}/entries")
    assert listing.status_code == 200
    first, second, _ = listing.json()
    assert first["claim_count"] == 7 and first["open_question_count"] == 0
    url = f"/entries/{first['id']}"
    assert client.post(url + "/approve", json={"approved_by": "Wrong"}).status_code == 422
    assert client.post(url + "/approve", json={"approved_by": EXPERT}).status_code == 200
    assert client.post(url + "/approve", json={"approved_by": EXPERT}).status_code == 409
    changed = client.patch(
        url, json={"edited_by": EXPERT, "sections": {"steps": {"claims": ["Restart twice."]}}}
    )
    assert changed.status_code == 200 and changed.json()["status"] == "draft"
    assert changed.json()["steps"]["claims"][0]["citations"][0]["kind"] == "interview_turn"
    assert [d["action"] for d in changed.json()["decisions"]] == ["approve", "edit"]
    assert client.post(url + "/approve", json={"approved_by": EXPERT}).status_code == 200
    rejected = client.post(
        f"/entries/{second['id']}/reject", json={"rejected_by": EXPERT, "reason": "Unneeded"}
    )
    assert rejected.status_code == 200 and rejected.json()["rejection_reason"] == "Unneeded"
    assert client.get(url).json()["status"] == "approved"
    assert client.post(f"/sessions/{session}/synthesise").status_code == 200
    assert client.get(url).json()["status"] == "approved"


async def test_api_missing_ids_and_unfinished_session(scratch: Scratch) -> None:
    client = TestClient(create_app(runbook_service=RunbookService(scratch.dsn)))
    for method, path, body in (
        ("get", f"/entries/{uuid4()}", None),
        ("get", f"/sessions/{uuid4()}/entries", None),
        ("post", f"/sessions/{uuid4()}/synthesise", None),
        ("post", f"/entries/{uuid4()}/approve", {"approved_by": EXPERT}),
        ("post", f"/entries/{uuid4()}/reject", {"rejected_by": EXPERT, "reason": "x"}),
        (
            "patch",
            f"/entries/{uuid4()}",
            {"edited_by": EXPERT, "sections": {"steps": {"claims": ["x"]}}},
        ),
    ):
        assert client.request(method, path, json=body).status_code == 404
    with scratch.connect() as conn:
        run = seed_run(conn)
    interview = make_service(scratch.dsn)
    plan = await interview.draft_plan(run)
    session = (await interview.approve(plan.id, EXPERT, expected_revision=plan.revision)).session.id
    assert client.post(f"/sessions/{session}/synthesise").status_code == 409


async def test_api_redacts_edits_reasons_and_invalid_request_echoes(scratch: Scratch) -> None:
    session = await finished(scratch)
    client = TestClient(create_app(runbook_service=RunbookService(scratch.dsn)))
    entry = client.get(f"/sessions/{session}/entries").json()[0]
    url = f"/entries/{entry['id']}"
    leak = f"password={FAKE_CREDENTIALS[1]}"
    bodies = []
    invalid_sections: tuple[dict[str, Any], ...] = (
        {},
        {"steps": {}},
        {"bad": {"claims": [leak]}},
        {leak: {"claims": [leak]}},
    )
    for sections in invalid_sections:
        response = client.patch(url, json={"edited_by": EXPERT, "sections": sections})
        assert response.status_code == 422
        bodies.append(response.text)
    changed = client.patch(
        url, json={"edited_by": EXPERT, "sections": {"steps": {"claims": [leak]}}}
    )
    assert changed.status_code == 200
    bodies.append(changed.text)
    assert client.post(url + "/reject", json={"rejected_by": EXPERT}).status_code == 422
    assert (
        client.post(url + "/reject", json={"rejected_by": EXPERT, "reason": " "}).status_code == 422
    )
    bodies.append(client.post(url + "/reject", json={"rejected_by": EXPERT, "reason": leak}).text)
    bodies.append(client.get(url).text)
    invalid = client.patch(
        url, json={"edited_by": EXPERT, "sections": {"steps": {"claims": [leak], "extra": leak}}}
    )
    assert invalid.status_code == 422
    bodies.append(invalid.text)
    assert all(FAKE_CREDENTIALS[1] not in body for body in bodies)


async def test_synthesise_keeps_edits_unless_asked_to_overwrite(scratch: Scratch) -> None:
    session = await finished(scratch)
    client = TestClient(create_app(runbook_service=RunbookService(scratch.dsn)))
    first = client.get(f"/sessions/{session}/entries").json()[0]
    assert first["edited"] is False
    url = f"/entries/{first['id']}"
    patch = {"edited_by": EXPERT, "sections": {"steps": {"claims": ["Restart twice."]}}}
    assert client.patch(url, json=patch).status_code == 200

    kept = client.post(f"/sessions/{session}/synthesise")
    assert kept.status_code == 200
    assert kept.json()[0]["edited"] is True
    assert client.get(url).json()["steps"]["claims"][0]["text"] == "Restart twice."

    replaced = client.post(f"/sessions/{session}/synthesise", params={"overwrite_edits": "true"})
    assert replaced.status_code == 200
    assert replaced.json()[0]["edited"] is False
    assert client.get(url).json()["steps"]["claims"][0]["text"] != "Restart twice."
    bad = client.post(f"/sessions/{session}/synthesise", params={"overwrite_edits": "maybe"})
    assert bad.status_code == 422


async def test_owner_sees_claims_the_cited_answer_does_not_support(scratch: Scratch) -> None:
    session = await finished(scratch)
    client = TestClient(create_app(runbook_service=RunbookService(scratch.dsn)))
    first = client.get(f"/sessions/{session}/entries").json()[0]
    assert first["unsupported_count"] == 7
    url = f"/entries/{first['id']}"
    detail = client.get(url).json()
    assert len(detail["unsupported"]) == 7
    assert detail["unsupported"][2] == {"section": "steps", "claim_index": 0, "terms": ["noted"]}
    patch = {"edited_by": EXPERT, "sections": {"steps": {"claims": ["Restart twice."]}}}
    changed = client.patch(url, json=patch).json()
    assert [u["section"] for u in changed["unsupported"]].count("steps") == 0
    assert client.get(f"/sessions/{session}/entries").json()[0]["unsupported_count"] == 6
    assert client.post(f"/sessions/{session}/synthesise").json()[1]["unsupported_count"] == 7
