"""Pooled connections: reuse, autocommit, and nothing session-scoped outlives a borrow."""

import time
from uuid import uuid4

import psycopg
import pytest

from eki import pool
from eki.interview import store
from tests.conftest import Scratch

pytestmark = pytest.mark.db


def backend(conn: psycopg.Connection) -> int:
    row = conn.execute("SELECT pg_backend_pid()").fetchone()
    assert row is not None
    return int(row[0])


def test_sequential_borrows_reuse_one_server_connection(scratch: Scratch) -> None:
    with pool.connection(scratch.dsn) as conn:
        first = backend(conn)
        assert conn.autocommit
    with pool.connection(scratch.dsn) as conn:
        assert backend(conn) == first


def test_a_session_lock_is_released_when_the_connection_is_returned(scratch: Scratch) -> None:
    key = uuid4()
    with pool.connection(scratch.dsn) as conn:
        # The interview service relies on closing a connection to release this lock.
        assert store.try_lock(conn, key)
    with scratch.connect() as other:
        assert store.try_lock(other, key)


def test_a_failed_borrow_does_not_leave_an_open_transaction(scratch: Scratch) -> None:
    with pytest.raises(RuntimeError), pool.connection(scratch.dsn) as conn:
        conn.execute("BEGIN")
        raise RuntimeError("boom")
    with pool.connection(scratch.dsn) as conn:
        assert conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE


def test_closing_a_pool_releases_its_connections(scratch: Scratch) -> None:
    with pool.connection(scratch.dsn) as conn:
        pid = backend(conn)
    pool.close(scratch.dsn)
    with scratch.connect() as other:
        # The server process exits shortly after the client disconnects.
        for _ in range(50):
            row = other.execute(
                "SELECT count(*) FROM pg_stat_activity WHERE pid = %s", (pid,)
            ).fetchone()
            if row is not None and row[0] == 0:
                break
            time.sleep(0.05)
        else:
            pytest.fail("the pooled connection is still open after close()")
    with pool.connection(scratch.dsn) as conn:  # a later borrow opens a new pool
        assert backend(conn) != pid
