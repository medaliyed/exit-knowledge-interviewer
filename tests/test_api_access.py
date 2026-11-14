"""F11: every route is gated; readers cannot use review/transcript routes as a bypass."""

import json
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from eki.access import set_owner, set_readers
from eki.api.app import create_app
from eki.auth import Principal, TokenAuthenticator
from eki.interview import store as interviews
from eki.runbook.service import RunbookService
from tests.conftest import Scratch
from tests.test_workflow_synthesis import finished

pytestmark = [pytest.mark.db, pytest.mark.anyio]
OWNER = Principal("owner", "Nadia Ferrand")
ADMIN = Principal("admin", "Operator", True)
TOKENS = {
    s: f"test-only-{s}-bearer-credential-00000001" for s in ("owner", "reader", "stranger", "admin")
}


def authenticator() -> TokenAuthenticator:
    return TokenAuthenticator(
        json.dumps(
            [
                {
                    "token": token,
                    "subject": s,
                    "name": "Nadia Ferrand" if s == "owner" else s,
                    "admin": s == "admin",
                }
                for s, token in TOKENS.items()
            ]
        )
    )


def headers(subject: str) -> dict[str, str]:
    return {"Authorization": "Bearer " + TOKENS[subject]}


async def test_approved_reader_cannot_read_drafts_or_interview_data(scratch: Scratch) -> None:
    session = await finished(scratch)
    books = RunbookService(scratch.dsn)
    first, draft, _ = books.list(session)
    approved = books.approve(UUID(first.entry.id), OWNER.name)
    with scratch.connect() as conn:
        view = interviews.load_session(conn, session)
        assert view is not None
        set_owner(conn, view.run_id, OWNER.subject, ADMIN)
        set_readers(conn, view.run_id, ["reader"], OWNER)
    client = TestClient(create_app(runbook_service=books, authenticator=authenticator()))
    url = f"/entries/{approved.entry.id}"
    for credential in ({}, {"Authorization": "Bearer invalid"}):
        assert client.get(url, headers=credential).status_code == 401
    for subject in ("owner", "reader", "admin"):
        response = client.get(url, headers=headers(subject))
        assert response.status_code == 200, response.text
        if subject == "reader":
            assert "decisions" not in response.json()
    assert client.get(url, headers=headers("stranger")).status_code == 404
    for path in (
        f"/entries/{draft.entry.id}",
        f"/sessions/{session}",
        f"/sessions/{session}/entries",
        f"/plans/{view.plan_id}",
    ):
        assert client.get(path, headers=headers("reader")).status_code == 404
    assert (
        client.post(
            url + "/approve", headers=headers("reader"), json={"approved_by": OWNER.name}
        ).status_code
        == 404
    )
    assert client.get(f"/runs/{view.run_id}/access", headers=headers("reader")).status_code == 404
    assert (
        client.put(
            f"/runs/{view.run_id}/access", headers=headers("owner"), json={"readers": []}
        ).status_code
        == 200
    )
    assert client.get(url, headers=headers("reader")).status_code == 404


async def test_owner_cannot_assign_ownership_or_spoof_reviewer(scratch: Scratch) -> None:
    session = await finished(scratch)
    books = RunbookService(scratch.dsn)
    entry = books.list(session)[0]
    with scratch.connect() as conn:
        view = interviews.load_session(conn, session)
        assert view is not None
        set_owner(conn, view.run_id, "owner", ADMIN)
    client = TestClient(create_app(runbook_service=books, authenticator=authenticator()))
    assert (
        client.put(
            f"/runs/{view.run_id}/owner", headers=headers("owner"), json={"subject": "reader"}
        ).status_code
        == 403
    )
    assert (
        client.post(
            f"/entries/{entry.entry.id}/approve",
            headers=headers("admin"),
            json={"approved_by": OWNER.name},
        ).status_code
        == 422
    )
    assert (
        client.post(
            f"/entries/{entry.entry.id}/approve",
            headers=headers("owner"),
            json={"approved_by": OWNER.name},
        ).status_code
        == 200
    )
    assert (
        client.get(f"/runs/{view.run_id}/access", headers=headers("owner")).json()["owner"]
        == "owner"
    )


def test_default_app_fails_closed_without_credentials() -> None:
    client = TestClient(create_app())
    assert client.get("/healthz").status_code == 200
    assert client.get("/entries/00000000-0000-0000-0000-000000000000").status_code == 401
