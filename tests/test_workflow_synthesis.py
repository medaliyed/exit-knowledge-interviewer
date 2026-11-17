"""F9: automatic synthesis and retry safety over the actual checkpointed workflow."""

from uuid import UUID

import pytest

from eki.errors import ConflictError
from eki.interview import store as interview_store
from eki.runbook import store
from eki.runbook.service import RunbookService
from tests.conftest import Scratch
from tests.interview_support import make_service, say, seed_run

pytestmark = [pytest.mark.db, pytest.mark.anyio]


async def finished(scratch: Scratch) -> UUID:
    with scratch.connect() as conn:
        run = seed_run(conn)
    service = make_service(scratch.dsn)
    plan = await service.draft_plan(run)
    session = (
        await service.approve(plan.id, "Nadia Ferrand", expected_revision=plan.revision)
    ).session.id
    for _ in plan.topics:
        await say(service, session, "Recorded answer.")
    return session


async def test_entries_are_created_automatically_and_repeat_safely(scratch: Scratch) -> None:
    session = await finished(scratch)
    service = RunbookService(scratch.dsn)
    first = service.list(session)
    assert len(first) == 3
    assert all(e.entry.status == "draft" for e in first)
    assert all(not e.entry.missing_sections() for e in first)
    again = service.synthesise(session)
    assert [e.entry for e in again] == [e.entry for e in first]
    with scratch.connect() as conn:
        assert interview_store.load_session(conn, session).status == "finished"  # type: ignore[union-attr]
        assert len(store.list_entries(conn, session)) == 3


async def test_unfinished_session_cannot_be_synthesised(scratch: Scratch) -> None:
    with scratch.connect() as conn:
        run = seed_run(conn)
    service = make_service(scratch.dsn)
    plan = await service.draft_plan(run)
    session = (
        await service.approve(plan.id, "Nadia Ferrand", expected_revision=plan.revision)
    ).session.id
    with pytest.raises(ConflictError, match="finished"):
        RunbookService(scratch.dsn).synthesise(session)


async def test_restart_during_synthesis_rolls_back_and_retries(
    scratch: Scratch, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = store.save_entry
    count = 0

    def failing(*args: object, **kwargs: object) -> store.StoredEntry:
        nonlocal count
        count += 1
        if count == 2:
            raise RuntimeError("simulated process failure")
        return original(*args, **kwargs)  # type: ignore[arg-type]

    with scratch.connect() as conn:
        run = seed_run(conn)
    service = make_service(scratch.dsn)
    plan = await service.draft_plan(run)
    session = (
        await service.approve(plan.id, "Nadia Ferrand", expected_revision=plan.revision)
    ).session.id
    for _ in range(2):
        await say(service, session, "Answer.")
    monkeypatch.setattr(store, "save_entry", failing)
    with pytest.raises(RuntimeError):
        await say(service, session, "Last answer.")
    with scratch.connect() as conn:
        assert store.list_entries(conn, session) == ()
    monkeypatch.setattr(store, "save_entry", original)
    stream = await make_service(scratch.dsn).resume(session)
    _ = [e async for e in stream]
    assert len(RunbookService(scratch.dsn).list(session)) == 3


async def test_restart_after_synthesis_commit_can_finish_checkpoint(
    scratch: Scratch, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typing import Any

    from eki.graph import workflow

    original = workflow._synthesise

    async def fail_after_commit(
        deps: workflow.Deps, state: workflow.InterviewState
    ) -> dict[str, Any]:
        await original(deps, state)
        raise RuntimeError("process stopped before checkpoint")

    with scratch.connect() as conn:
        run = seed_run(conn)
    service = make_service(scratch.dsn)
    plan = await service.draft_plan(run)
    session = (
        await service.approve(plan.id, "Nadia Ferrand", expected_revision=plan.revision)
    ).session.id
    for _ in range(2):
        await say(service, session, "Answer.")
    monkeypatch.setattr(workflow, "_synthesise", fail_after_commit)
    with pytest.raises(RuntimeError):
        await say(service, session, "Last answer.")
    before = RunbookService(scratch.dsn).list(session)
    assert len(before) == 3
    monkeypatch.setattr(workflow, "_synthesise", original)
    events = [e async for e in await make_service(scratch.dsn).resume(session)]
    assert [e["type"] for e in events] == ["delivery", "recovered", "done"]
    assert [e.entry for e in RunbookService(scratch.dsn).list(session)] == [e.entry for e in before]
