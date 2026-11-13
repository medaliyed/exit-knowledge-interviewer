"""Deliverable f (F2): the stored snapshot is complete, isolated, atomic and free of secrets."""

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime

import psycopg
import pytest
from mcp import Client
from psycopg import sql

from eki.config import get_settings
from eki.db import connect, migrate
from eki.ingest.models import Snapshot
from eki.ingest.pipeline import IngestError, load, pull, store
from mcp_servers.git_server import build_server as build_git
from mcp_servers.tickets_server import build_server as build_tickets
from tests.support import CORPUS, FAKE_CREDENTIALS, FULL_SPAN

pytestmark = [pytest.mark.db, pytest.mark.anyio]


@pytest.fixture
def conn() -> Iterator[psycopg.Connection]:
    schema = f"test_{uuid.uuid4().hex[:12]}"
    connection = connect(get_settings())
    connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    connection.execute(sql.SQL("SET search_path TO {}, public").format(sql.Identifier(schema)))
    connection.commit()
    migrate(connection)
    try:
        yield connection
    finally:
        connection.rollback()
        connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
        connection.commit()
        connection.close()


@pytest.fixture
async def snapshot() -> Snapshot:
    async with (
        Client(build_git(FULL_SPAN, CORPUS), raise_exceptions=True) as git,
        Client(build_tickets(FULL_SPAN, CORPUS), raise_exceptions=True) as tickets,
    ):
        return await pull(git, tickets, FULL_SPAN)


async def test_store_then_load_round_trips(conn: psycopg.Connection, snapshot: Snapshot) -> None:
    run_id = store(conn, snapshot)
    assert load(conn, run_id) == snapshot
    status = conn.execute("SELECT status, finished_at IS NOT NULL FROM ingest_runs").fetchone()
    assert status == ("completed", True)


async def test_no_secret_in_any_column(conn: psycopg.Connection, snapshot: Snapshot) -> None:
    store(conn, snapshot)
    tables = [
        row[0]
        for row in conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema()"
        ).fetchall()
    ]
    assert {"artifacts", "activity", "ingest_runs"} <= set(tables)
    for table in tables:
        rows = conn.execute(
            sql.SQL("SELECT to_jsonb(t)::text FROM {} t").format(sql.Identifier(table))
        ).fetchall()
        dumped = "\n".join(row[0] for row in rows)
        for secret in FAKE_CREDENTIALS:
            assert secret not in dumped, f"{secret} found in {table}"
    assert conn.execute(
        "SELECT count(*) FROM artifacts WHERE body LIKE '%[REDACTED:credential]%'"
    ).fetchone() == (2,)


async def test_two_runs_are_independent(conn: psycopg.Connection, snapshot: Snapshot) -> None:
    first, second = store(conn, snapshot), store(conn, snapshot)
    assert first != second
    count = len(snapshot.artifacts)
    for run_id in (first, second):
        assert conn.execute(
            "SELECT count(*) FROM artifacts WHERE run_id = %s", (run_id,)
        ).fetchone() == (count,)
    conn.execute("DELETE FROM ingest_runs WHERE id = %s", (first,))
    assert load(conn, second) == snapshot


async def test_failed_store_leaves_no_partial_run(
    conn: psycopg.Connection, snapshot: Snapshot
) -> None:
    duplicated = snapshot.model_copy(
        update={"artifacts": (*snapshot.artifacts, snapshot.artifacts[0])}
    )
    with pytest.raises(IngestError):
        store(conn, duplicated)
    conn.rollback()
    assert conn.execute("SELECT count(*) FROM ingest_runs").fetchone() == (0,)
    assert conn.execute("SELECT count(*) FROM artifacts").fetchone() == (0,)


async def test_load_of_unknown_run_fails(conn: psycopg.Connection) -> None:
    with pytest.raises(IngestError):
        load(conn, uuid.uuid4())


async def test_empty_snapshot_round_trips(conn: psycopg.Connection) -> None:
    empty = Snapshot(
        expert_name="Nobody",
        expert_email="nobody@example.com",
        since=datetime(2025, 1, 1, tzinfo=UTC),
        until=datetime(2025, 2, 1, tzinfo=UTC),
        artifacts=(),
        activity=(),
    )
    run_id = store(conn, empty)
    assert load(conn, run_id) == empty
