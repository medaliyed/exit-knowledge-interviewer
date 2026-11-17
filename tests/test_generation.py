"""F18: completed topics survive late drafting failures and configuration changes."""

from collections.abc import Sequence
from uuid import UUID, uuid4

import pytest

from eki.errors import ConflictError, NotFoundError
from eki.interview import store
from eki.interview.service import BusyError
from eki.llm import Message, ModelError, Reply, ToolSpec
from eki.mapping.store import load_mapping, save_mapping
from tests.conftest import Scratch
from tests.fakes import ScriptedModel
from tests.interview_support import make_service, quoting_drafter, seed_run

pytestmark = [pytest.mark.db, pytest.mark.anyio]


def ten_topics(scratch: Scratch) -> UUID:
    with scratch.connect() as conn:
        run = seed_run(conn)
        topics, scores = load_mapping(conn, run)
        chosen = tuple(
            topics[0].model_copy(update={"id": f"topic:{i:02d}", "label": f"Topic {i + 1}"})
            for i in range(10)
        )
        ranked = tuple(
            scores[0].model_copy(update={"topic_id": t.id, "score": 90 - i})
            for i, t in enumerate(chosen)
        )
        save_mapping(conn, run, chosen, ranked)
    return run


async def test_late_failure_resumes_only_unfinished_topics(scratch: Scratch) -> None:
    run, gid = ten_topics(scratch), uuid4()
    attempts = {"count": 0}

    def policy(messages: Sequence[Message], tools: Sequence[ToolSpec]) -> Reply:
        attempts["count"] += 1
        if attempts["count"] == 9:
            raise ModelError("failed ninth topic")
        return quoting_drafter(messages, tools)

    drafter = ScriptedModel(policy, name="drafter")
    service = make_service(scratch.dsn, drafter=drafter)
    with pytest.raises(ModelError):
        await service.draft_plan(run, generation_id=gid)
    partial = await service.generation_status(run, gid)
    assert (partial["status"], partial["completed"], partial["total"]) == ("needs_retry", 8, 10)
    saved = partial["topics"][:8]
    with pytest.raises(NotFoundError):
        await service.approve(gid, "Nadia Ferrand")
    restarted = make_service(scratch.dsn, drafter=drafter)
    plan = await restarted.draft_plan(run, generation_id=gid)
    assert plan.id == gid and len(plan.topics) == 10
    assert len(drafter.calls) == 11, "eight successful calls were not repeated"
    complete = await restarted.generation_status(run, gid)
    assert complete["status"] == "completed"
    assert complete["topics"][:8] == saved
    assert await restarted.draft_plan(run, generation_id=gid) == plan
    assert len(drafter.calls) == 11


async def test_changed_model_refuses_to_mix_partial_results(scratch: Scratch) -> None:
    run, gid = ten_topics(scratch), uuid4()
    model = ScriptedModel([ModelError("down")], name="original")
    service = make_service(scratch.dsn, drafter=model)
    with pytest.raises(ModelError):
        await service.draft_plan(run, generation_id=gid)
    changed = make_service(scratch.dsn, drafter=ScriptedModel(quoting_drafter, name="changed"))
    with pytest.raises(ConflictError, match="changed"):
        await changed.draft_plan(run, generation_id=gid)
    assert (await service.generation_status(run, gid))["completed"] == 0


async def test_concurrent_generation_resume_is_serialized(scratch: Scratch) -> None:
    run, gid = ten_topics(scratch), uuid4()
    with scratch.connect() as conn:
        assert store.try_lock(conn, gid)
        with pytest.raises(BusyError):
            await make_service(scratch.dsn).draft_plan(run, generation_id=gid)


async def test_changed_inputs_and_cross_run_ids_are_refused(scratch: Scratch) -> None:
    run, gid = ten_topics(scratch), uuid4()
    service = make_service(scratch.dsn, drafter=ScriptedModel([ModelError("down")], name="drafter"))
    with pytest.raises(ModelError):
        await service.draft_plan(run, generation_id=gid)
    with scratch.connect() as conn:
        other = seed_run(conn)
    with pytest.raises(NotFoundError):
        await make_service(scratch.dsn).draft_plan(other, generation_id=gid)
    with scratch.connect() as conn:
        conn.execute("UPDATE topics SET label='A changed topic' WHERE run_id=%s", (run,))
    with pytest.raises(ConflictError, match="changed"):
        await make_service(scratch.dsn).draft_plan(run, generation_id=gid)


async def test_changed_prompt_is_detected(
    scratch: Scratch, monkeypatch: pytest.MonkeyPatch
) -> None:
    from eki.ingest.redaction import RedactedText

    run, gid = ten_topics(scratch), uuid4()
    service = make_service(scratch.dsn, drafter=ScriptedModel([ModelError("down")], name="drafter"))
    with pytest.raises(ModelError):
        await service.draft_plan(run, generation_id=gid)
    monkeypatch.setattr("eki.interview.drafting.SYSTEM", RedactedText("A changed system prompt."))
    with pytest.raises(ConflictError, match="changed"):
        await make_service(scratch.dsn).draft_plan(run, generation_id=gid)


def test_api_reports_partial_generation_and_resumes_it(scratch: Scratch) -> None:
    from fastapi.testclient import TestClient

    from tests.api_support import create_app

    calls = {"count": 0}

    def policy(messages: Sequence[Message], tools: Sequence[ToolSpec]) -> Reply:
        calls["count"] += 1
        if calls["count"] == 2:
            raise ModelError("second topic unavailable")
        return quoting_drafter(messages, tools)

    with scratch.connect() as conn:
        run = seed_run(conn)
    gid = uuid4()
    client = TestClient(
        create_app(make_service(scratch.dsn, drafter=ScriptedModel(policy, name="drafter")))
    )
    failed = client.post(f"/runs/{run}/plan?generation_id={gid}")
    assert failed.status_code == 502 and failed.json()["generation_id"] == str(gid)
    status = client.get(f"/runs/{run}/generations/{gid}").json()
    assert status["completed"] == 1 and status["total"] == 3
    assert client.get(f"/runs/{run}/generations").json()[0]["id"] == str(gid)
    assert (
        client.post(f"/plans/{gid}/approve", json={"approved_by": "Nadia Ferrand"}).status_code
        == 404
    )
    response = client.post(f"/runs/{run}/generations/{gid}/resume")
    assert response.status_code == 200 and response.json()["id"] == str(gid)
    assert calls["count"] == 4
    assert client.post(f"/runs/{run}/generations/{uuid4()}/resume").status_code == 404


def test_cli_resume_and_model_free_status(
    scratch: Scratch, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from eki.cli import main
    from eki.config import get_settings

    calls = {"count": 0}

    def policy(messages: Sequence[Message], tools: Sequence[ToolSpec]) -> Reply:
        calls["count"] += 1
        if calls["count"] == 2:
            raise ModelError("second topic unavailable")
        return quoting_drafter(messages, tools)

    with scratch.connect() as conn:
        run = seed_run(conn)
    service = make_service(scratch.dsn, drafter=ScriptedModel(policy, name="drafter"))
    monkeypatch.setattr("eki.cli.interview.build_service", lambda settings: service)
    monkeypatch.setenv("EKI_DATABASE_URL", scratch.dsn)
    get_settings.cache_clear()
    try:
        assert main(["plan", "--run", str(run)]) == 1
        assert "completed topics are saved" in capsys.readouterr().err
        with scratch.connect() as conn:
            row = conn.execute("SELECT id FROM plan_generations").fetchone()
            assert row is not None
            gid = row[0]
        assert main(["plan", "--run", str(run), "--generation-status", str(gid)]) == 0
        assert '"completed": 1' in capsys.readouterr().out
        assert main(["plan", "--run", str(run), "--resume-generation", str(gid)]) == 0
        assert calls["count"] == 4
    finally:
        get_settings.cache_clear()


async def test_drafting_repair_prompts_do_not_repeat_a_secret_from_a_bad_id(
    scratch: Scratch,
) -> None:
    import json

    from eki.graph.workflow import Limits
    from tests.support import FAKE_CREDENTIALS, tables_containing

    with scratch.connect() as conn:
        run = seed_run(conn)
    model = ScriptedModel(
        [
            Reply(
                json.dumps(
                    {
                        "questions": [
                            {
                                "artifact_id": f"password={FAKE_CREDENTIALS[1]}",
                                "quote": "Not an artifact",
                                "text": "What happened?",
                            }
                        ]
                    }
                )
            ),
            Reply('{"questions": []}'),
        ],
        name="drafter",
    )
    service = make_service(scratch.dsn, drafter=model, limits=Limits(plan_size=1))
    await service.draft_plan(run)
    assert FAKE_CREDENTIALS[1] not in str(model.calls)
    with scratch.connect() as conn:
        assert tables_containing(conn, scratch.schema, FAKE_CREDENTIALS[1]) == []


async def test_publication_survives_a_crash_before_its_graph_checkpoint(
    scratch: Scratch, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typing import Any

    from eki.graph import workflow

    run, gid = ten_topics(scratch), uuid4()
    model = ScriptedModel(quoting_drafter, name="drafter")
    service = make_service(scratch.dsn, drafter=model)
    original = workflow._draft_plan

    async def crash(deps: workflow.Deps, state: workflow.InterviewState) -> dict[str, Any]:
        await original(deps, state)
        raise RuntimeError("publication committed before checkpoint")

    monkeypatch.setattr(workflow, "_draft_plan", crash)
    with pytest.raises(RuntimeError):
        await service.draft_plan(run, generation_id=gid)
    assert (await service.generation_status(run, gid))["status"] == "completed"
    monkeypatch.setattr(workflow, "_draft_plan", original)
    assert (await service.draft_plan(run, generation_id=gid)).id == gid
    assert len(model.calls) == 10


def test_generation_inspection_and_resume_are_owner_only(scratch: Scratch) -> None:
    from fastapi.testclient import TestClient

    from eki.access import set_owner, set_readers
    from eki.api.app import create_app
    from tests.test_api_access import ADMIN, OWNER, authenticator, headers

    with scratch.connect() as conn:
        own, other = seed_run(conn), seed_run(conn)
        set_owner(conn, own, OWNER.subject, ADMIN)
        set_readers(conn, own, ["reader"], OWNER)
        set_owner(conn, other, "stranger", ADMIN)
    service = make_service(scratch.dsn, drafter=ScriptedModel([ModelError("down")], name="drafter"))
    client = TestClient(create_app(service, authenticator=authenticator()))
    gid = uuid4()
    failed = client.post(f"/runs/{own}/plan?generation_id={gid}", headers=headers("owner"))
    assert failed.status_code == 502
    for role in ("reader", "stranger"):
        assert (
            client.get(f"/runs/{own}/generations/{gid}", headers=headers(role)).status_code == 404
        )
        assert (
            client.post(f"/runs/{own}/generations/{gid}/resume", headers=headers(role)).status_code
            == 404
        )
    assert (
        client.post(
            f"/runs/{other}/generations/{gid}/resume", headers=headers("stranger")
        ).status_code
        == 404
    )
    state = client.get(f"/runs/{own}/generations/{gid}", headers=headers("owner")).json()
    assert state["status"] == "needs_retry"
