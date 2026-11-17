"""F11: durable background indexing, provider isolation and crash-safe publication."""

import asyncio
import json
import subprocess
import sys
import threading
from collections.abc import Sequence

import pytest

from eki import pool
from eki.ingest.embedding import HashingEmbedder
from eki.ingest.redaction import RedactedText
from eki.llm import ModelError
from eki.runbook.review import SectionEdit
from eki.runbook.service import RunbookService
from eki.search.answers import AnswerService
from eki.search.jobs import IndexWorker
from eki.search.service import SearchService
from tests.conftest import Scratch
from tests.search_support import OWNER, READER, STRANGER, search_world

pytestmark = [pytest.mark.db, pytest.mark.anyio]


async def test_approval_queues_work_and_search_never_indexes_it(scratch: Scratch) -> None:
    world = await search_world(scratch, indexed=False)
    search = SearchService(scratch.dsn)
    assert search.search(READER, "reconciliation") == []
    assert search.indexing(READER)["pending_count"] == 2
    assert search.indexing(STRANGER)["entries"] == {}
    answer = await AnswerService(search).ask(READER, "reconciliation")
    assert answer.reason == "indexing"
    assert IndexWorker(scratch.dsn, search.embedder).drain()["ready"] == 2
    assert search.indexing(READER)["pending_count"] == 0
    assert search.search(READER, "reconciliation")[0].entry.id == str(world.billing)
    with scratch.connect() as conn:
        assert conn.execute(
            "SELECT count(*) FROM runbook_index_jobs WHERE entry_id=%s", (world.draft,)
        ).fetchone() == (0,)


async def test_failure_backoff_and_complete_atomic_retry(scratch: Scratch) -> None:
    world = await search_world(scratch, indexed=False)
    books = RunbookService(scratch.dsn)
    books.edit(world.gateway, OWNER.name, {"steps": SectionEdit(claims=("Draft",))})

    class Flaky(HashingEmbedder):
        fail = True

        def embed(self, texts: Sequence[RedactedText]) -> list[list[float]]:
            if self.fail:
                raise ModelError("private provider payload must not be saved")
            return super().embed(texts)

    embedder = Flaky()
    worker = IndexWorker(scratch.dsn, embedder)
    worker.register()
    assert worker.process_one() == "retrying"
    assert worker.process_one() is None
    with scratch.connect() as conn:
        assert conn.execute("SELECT count(*) FROM runbook_index").fetchone() == (0,)
        row = conn.execute(
            "SELECT status,error_code,attempts FROM runbook_index_jobs WHERE entry_id=%s",
            (world.billing,),
        ).fetchone()
        assert row == ("retrying", "provider_unavailable", 1)
        conn.execute("UPDATE runbook_index_jobs SET available_at=now()")
    embedder.fail = False
    assert worker.process_one() == "ready"
    assert SearchService(scratch.dsn).search(READER, "reconciliation")


async def test_concurrent_workers_do_not_duplicate_provider_work(scratch: Scratch) -> None:
    world = await search_world(scratch, indexed=False)
    RunbookService(scratch.dsn).edit(
        world.gateway, OWNER.name, {"steps": SectionEdit(claims=("Draft",))}
    )
    started, release = threading.Event(), threading.Event()

    class Slow(HashingEmbedder):
        calls = 0

        def embed(self, texts: Sequence[RedactedText]) -> list[list[float]]:
            self.calls += 1
            started.set()
            assert release.wait(5)
            return super().embed(texts)

    embedder = Slow()
    worker = IndexWorker(scratch.dsn, embedder)
    task = asyncio.create_task(asyncio.to_thread(worker.process_one))
    try:
        assert await asyncio.to_thread(started.wait, 5)
        assert await asyncio.to_thread(IndexWorker(scratch.dsn, embedder).process_one) is None
        assert embedder.calls == 1
    finally:
        release.set()
    assert await task == "ready"


async def test_edit_during_embedding_cannot_publish_stale_content(scratch: Scratch) -> None:
    world = await search_world(scratch, indexed=False)
    books = RunbookService(scratch.dsn)
    books.edit(world.gateway, OWNER.name, {"steps": SectionEdit(claims=("Draft",))})

    class Racing(HashingEmbedder):
        def embed(self, texts: Sequence[RedactedText]) -> list[list[float]]:
            books.edit(world.billing, OWNER.name, {"steps": SectionEdit(claims=("New draft",))})
            return super().embed(texts)

    assert IndexWorker(scratch.dsn, Racing()).process_one() == "obsolete"
    with scratch.connect() as conn:
        assert conn.execute("SELECT count(*) FROM runbook_index").fetchone() == (0,)
    books.approve(world.billing, OWNER.name)
    assert IndexWorker(scratch.dsn, HashingEmbedder()).drain()["ready"] == 1


async def test_worker_and_query_provider_calls_do_not_hold_request_pool_connections(
    scratch: Scratch, monkeypatch: pytest.MonkeyPatch
) -> None:
    await search_world(scratch, indexed=False)
    pool.close(scratch.dsn)
    monkeypatch.setattr(pool, "MAX_SIZE", 1)

    class Borrowing(HashingEmbedder):
        def embed(self, texts: Sequence[RedactedText]) -> list[list[float]]:
            # A size-one pool would time out if the caller held a connection here.
            with pool.connection(scratch.dsn) as conn:
                assert conn.execute("SELECT 1").fetchone() == (1,)
            return super().embed(texts)

    embedder = Borrowing()
    assert IndexWorker(scratch.dsn, embedder).drain()["ready"] == 2
    assert SearchService(scratch.dsn, embedder=embedder).search(READER, "reconciliation")


async def test_process_death_releases_claim_for_another_worker(scratch: Scratch) -> None:
    await search_world(scratch, indexed=False)
    script = """
import json, os, sys
from eki.ingest.embedding import HashingEmbedder
from eki.search.jobs import IndexWorker
class Dies(HashingEmbedder):
    def embed(self, texts):
        os._exit(0)
IndexWorker(json.load(sys.stdin)['dsn'], Dies()).process_one()
raise RuntimeError('worker did not claim a job')
"""
    await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-c", script],
        input=json.dumps({"dsn": scratch.dsn}),
        text=True,
        check=True,
        capture_output=True,
        timeout=30,
    )
    with scratch.connect() as conn:
        assert conn.execute(
            "SELECT count(*) FROM runbook_index_jobs WHERE status='processing'"
        ).fetchone() == (1,)
    assert IndexWorker(scratch.dsn, HashingEmbedder()).drain()["ready"] == 2


async def test_new_model_is_backfilled_separately(scratch: Scratch) -> None:
    world = await search_world(scratch)

    class Other(HashingEmbedder):
        name = "test-other-space"

    search = SearchService(scratch.dsn, embedder=Other())
    assert search.search(READER, "reconciliation") == []
    assert IndexWorker(scratch.dsn, search.embedder).drain()["ready"] == 2
    assert search.search(READER, "reconciliation")[0].entry.id == str(world.billing)


async def test_api_lifespan_processes_queue_and_hides_private_status(scratch: Scratch) -> None:
    from fastapi.testclient import TestClient

    from eki.api.app import create_app
    from tests.test_api_access import authenticator, headers

    world = await search_world(scratch, indexed=False)
    with TestClient(
        create_app(search_service=SearchService(scratch.dsn), authenticator=authenticator())
    ) as client:
        for _ in range(100):
            response = client.get("/runbook/indexing", headers=headers("reader"))
            assert response.status_code == 200
            if response.json()["pending_count"] == 0:
                break
            await asyncio.sleep(0.02)
        assert response.json()["entries"][str(world.billing)] == "ready"
        assert str(world.draft) not in response.text
        assert client.get("/runbook/indexing", headers=headers("stranger")).json()["entries"] == {}


async def test_approval_and_queued_job_roll_back_together(scratch: Scratch) -> None:
    from datetime import UTC, datetime

    from eki.runbook import review, store

    world = await search_world(scratch, indexed=False)
    books = RunbookService(scratch.dsn)
    draft = books.get(world.draft)
    with scratch.connect() as conn:
        with pytest.raises(RuntimeError), conn.transaction():
            approved = review.approve(draft.entry, OWNER.name, OWNER.name, datetime.now(UTC))
            store.save_entry(conn, draft.session_id, approved)
            assert conn.execute(
                "SELECT count(*) FROM runbook_index_jobs WHERE entry_id=%s", (world.draft,)
            ).fetchone() == (1,)
            raise RuntimeError("approval transaction failed")
        assert conn.execute(
            "SELECT count(*) FROM runbook_index_jobs WHERE entry_id=%s", (world.draft,)
        ).fetchone() == (0,)
    assert books.get(world.draft).entry.status == "draft"


async def test_audit_failure_does_not_call_the_embedding_provider(
    scratch: Scratch, monkeypatch: pytest.MonkeyPatch
) -> None:
    from eki.audit import AuditError, AuditRecorder
    from eki.search.embedding import AuditedEmbedder

    await search_world(scratch, indexed=False)

    class Uncalled(HashingEmbedder):
        def embed(self, texts: Sequence[RedactedText]) -> list[list[float]]:
            raise AssertionError("provider must not run without its audit")

    recorder = AuditRecorder(scratch.dsn)

    def unavailable(*args: object, **kwargs: object) -> None:
        raise AuditError("audit unavailable")

    monkeypatch.setattr(recorder, "record", unavailable)
    worker = IndexWorker(scratch.dsn, AuditedEmbedder(Uncalled(), recorder))
    assert worker.process_one() == "retrying"
    with scratch.connect() as conn:
        assert conn.execute(
            "SELECT error_code FROM runbook_index_jobs WHERE status='retrying'"
        ).fetchone() == ("audit_unavailable",)


async def test_operator_cli_drains_jobs_without_a_bearer_token(
    scratch: Scratch, capsys: pytest.CaptureFixture[str]
) -> None:
    from argparse import Namespace

    from eki.cli.knowledge import index_jobs
    from eki.config import Settings

    await search_world(scratch, indexed=False)
    settings = Settings(_env_file=None, database_url=scratch.dsn)
    args = Namespace(once=True, limit=1000)
    assert index_jobs(args, settings) == 0
    assert json.loads(capsys.readouterr().out)["ready"] == 2
    assert index_jobs(args, settings) == 0
    assert json.loads(capsys.readouterr().out)["ready"] == 0


async def test_migration_backfills_existing_approvals_and_keeps_valid_indexes(
    scratch: Scratch,
) -> None:
    from eki.db import migrate

    world = await search_world(scratch)
    with scratch.connect() as conn:
        # Recreate the pre-0019 schema inside this disposable test only.
        conn.execute("DROP TRIGGER runbook_index_enqueue_insert ON runbook_entries")
        conn.execute("DROP TRIGGER runbook_index_enqueue_update ON runbook_entries")
        conn.execute("DROP FUNCTION enqueue_runbook_index()")
        conn.execute("DROP TABLE runbook_index_jobs")
        conn.execute("DROP TABLE runbook_index_models")
        conn.execute("DELETE FROM schema_migrations WHERE name='0019_runbook_index_jobs.sql'")
        conn.execute("DELETE FROM runbook_index WHERE entry_id=%s", (world.billing,))
        assert migrate(conn) == ["0019_runbook_index_jobs.sql"]
        assert conn.execute(
            "SELECT status FROM runbook_index_jobs WHERE entry_id=%s", (world.gateway,)
        ).fetchone() == ("ready",)
        assert conn.execute(
            "SELECT status FROM runbook_index_jobs WHERE entry_id=%s", (world.billing,)
        ).fetchone() == ("pending",)
    assert IndexWorker(scratch.dsn, HashingEmbedder()).drain()["ready"] == 1
