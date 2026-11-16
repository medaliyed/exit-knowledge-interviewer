"""F22 review fixes: abstention precedence, plan revision naming, draft discard, clocks."""

import json
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest

from eki.auth import acting_as
from eki.errors import ConflictError, NotFoundError
from eki.followup.gaps import GapService
from eki.followup.sessions import FollowUpService
from tests.conftest import Scratch
from tests.fakes import ScriptedModel, tool_reply
from tests.followup_support import ask, prepare, triaged_world
from tests.interview_support import make_service
from tests.search_support import ADMIN, OWNER, READER
from tests.test_follow_up_sessions import IMPOSTOR

pytestmark = [pytest.mark.db, pytest.mark.anyio]


# --- a context limit stays a context limit even when an overdue entry also matches ---


async def test_context_limit_is_not_hidden_by_review_required(
    scratch: Scratch, monkeypatch: pytest.MonkeyPatch
) -> None:
    from eki.search import answers as module
    from eki.search.answers import AnswerService
    from eki.search.service import SearchService
    from tests.followup_support import gap_world

    world = await gap_world(scratch)
    original = module._candidates

    def truncated(hits: Any, question: str) -> Any:
        return [item for item in original(hits, question) if item[1].claim_index == 0][:1]

    monkeypatch.setattr(module, "_candidates", truncated)
    monkeypatch.setattr(SearchService, "has_overdue", lambda self, principal, query: True)
    with scratch.connect() as conn:
        conn.execute(
            "UPDATE runbook_entries SET content=jsonb_set(content,'{steps,claims}',"
            "(content->'steps'->'claims') || (content->'steps'->'claims')) WHERE id=%s",
            (world.ledger,),
        )
    answer = await AnswerService(SearchService(scratch.dsn)).ask(
        READER, "What are the steps for incident:ledger:drift?"
    )
    assert answer.status == "insufficient_evidence"
    assert answer.reason == "context_limit"


# --- plan approval takes an explicitly named plan revision ---


async def test_plan_approval_uses_expected_revision(scratch: Scratch) -> None:
    from fastapi.testclient import TestClient

    from eki.api.app import create_app
    from eki.config import Settings

    world, rollback_gap, _ = await triaged_world(scratch)
    plan = prepare(scratch, world, [rollback_gap])
    seed = plan.topics[0].questions[0].text
    model = ScriptedModel([tool_reply(ask(seed, "rollback", rollback_gap))], name="interviewer")
    interviews = make_service(scratch.dsn, agent=None, interviewer=model)
    token = "fix-owner-token-0000000000000000000001"
    settings = Settings(
        _env_file=None,
        database_url=scratch.dsn,
        api_tokens=json.dumps([{"token": token, "subject": OWNER.subject, "name": OWNER.name}]),
    )
    client = TestClient(create_app(interviews, settings=settings))
    owner = {"Authorization": "Bearer " + token}
    stale = client.post(
        f"/plans/{plan.id}/approve",
        headers=owner,
        json={"approved_by": OWNER.name, "expected_revision": plan.revision + 1},
    )
    assert stale.status_code == 409, stale.text
    approved = client.post(
        f"/plans/{plan.id}/approve",
        headers=owner,
        json={"approved_by": OWNER.name, "expected_revision": plan.revision},
    )
    assert approved.status_code == 200, approved.text


# --- a draft follow-up can be discarded explicitly ---


async def test_draft_follow_up_is_discarded_explicitly(scratch: Scratch) -> None:
    world, rollback_gap, request_gap = await triaged_world(scratch)
    plan = prepare(scratch, world, [rollback_gap, request_gap])
    follow = FollowUpService(scratch.dsn)
    for who in (READER, IMPOSTOR):
        with pytest.raises(NotFoundError):
            follow.discard(plan.id, who, expected_revision=plan.revision)
    with pytest.raises(ConflictError):
        follow.discard(plan.id, OWNER, expected_revision=plan.revision + 1)
    discarded = follow.discard(plan.id, OWNER, expected_revision=plan.revision)
    assert discarded.topics == ()
    assert follow.discard(plan.id, OWNER, expected_revision=discarded.revision) == discarded
    statuses = {
        g["id"]: g["status"] for g in GapService(scratch.dsn).queue(world.run_id, OWNER)["gaps"]
    }
    assert statuses == {rollback_gap: "pending", request_gap: "pending"}
    again = prepare(scratch, world, [rollback_gap], request_id=uuid4())
    assert again.kind == "follow_up"


async def test_approved_follow_up_cannot_be_discarded(scratch: Scratch) -> None:
    world, rollback_gap, _ = await triaged_world(scratch)
    plan = prepare(scratch, world, [rollback_gap])
    seed = plan.topics[0].questions[0].text
    model = ScriptedModel([tool_reply(ask(seed, "rollback", rollback_gap))], name="interviewer")
    interviews = make_service(scratch.dsn, agent=None, interviewer=model)
    with acting_as(OWNER):
        await interviews.approve(plan.id, OWNER.name)
    current = await interviews.get_plan(plan.id)
    with pytest.raises(ConflictError, match="stop"):
        FollowUpService(scratch.dsn).discard(plan.id, ADMIN, expected_revision=current.revision)


async def test_http_discard_route(scratch: Scratch) -> None:
    from fastapi.testclient import TestClient

    from eki.api.app import create_app
    from eki.config import Settings

    world, rollback_gap, _ = await triaged_world(scratch)
    plan = prepare(scratch, world, [rollback_gap])
    reader_token = "fix-reader-token-000000000000000000001"
    owner_token = "fix-owner-token-0000000000000000000002"
    settings = Settings(
        _env_file=None,
        database_url=scratch.dsn,
        api_tokens=json.dumps(
            [
                {"token": reader_token, "subject": READER.subject, "name": READER.name},
                {"token": owner_token, "subject": OWNER.subject, "name": OWNER.name},
            ]
        ),
    )
    client = TestClient(create_app(make_service(scratch.dsn), settings=settings))
    body = {"expected_revision": plan.revision}
    assert (
        client.post(
            f"/plans/{plan.id}/discard",
            headers={"Authorization": "Bearer " + reader_token},
            json=body,
        ).status_code
        == 404
    )
    done = client.post(
        f"/plans/{plan.id}/discard",
        headers={"Authorization": "Bearer " + owner_token},
        json=body,
    )
    assert done.status_code == 200, done.text
    assert done.json()["topics"] == []


# --- finishing uses the service clock ---


async def test_finish_records_attempts_with_the_service_clock(scratch: Scratch) -> None:
    world, rollback_gap, _ = await triaged_world(scratch)
    plan = prepare(scratch, world, [rollback_gap])
    seed = plan.topics[0].questions[0].text
    model = ScriptedModel([tool_reply(ask(seed, "rollback", rollback_gap))], name="interviewer")
    interviews = make_service(scratch.dsn, agent=None, interviewer=model)
    frozen = datetime(2030, 1, 2, 3, 4, 5, tzinfo=UTC)
    follow = FollowUpService(scratch.dsn, clock=lambda: frozen)
    with acting_as(OWNER):
        view = await interviews.approve(plan.id, OWNER.name)
        follow.stop(view.session.id, OWNER)
    (gap,) = [
        g
        for g in GapService(scratch.dsn).queue(world.run_id, OWNER, filter="all")["gaps"]
        if g["id"] == rollback_gap
    ]
    assert [a["recorded_at"] for a in gap["attempts"]] == [frozen.isoformat()]
