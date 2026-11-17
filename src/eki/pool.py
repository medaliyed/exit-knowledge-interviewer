"""Pooled Postgres connections for short request work, one pool per connection string.

Every borrowed connection is in autocommit mode, as the per-call connections it replaces
were. A connection returned to the pool must look like a fresh one, so returning it
releases any session-level advisory lock (code that relied on closing a connection to
release ``pg_try_advisory_lock`` keeps working) and rolls back any open transaction.
Long-running work that holds a connection across model calls, such as the interview
workflow, keeps its own direct connections instead of occupying the pool.
"""

import atexit
import threading
from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
from psycopg_pool import ConnectionPool

MAX_SIZE = 10
"""Connections per pool. A borrow waits up to ``TIMEOUT`` seconds when all are in use."""
TIMEOUT = 5.0
MAX_IDLE = 300.0
"""Idle connections are closed after this many seconds; no connection is kept open eagerly."""

_pools: dict[str, ConnectionPool] = {}
_lock = threading.Lock()


def _release(conn: psycopg.Connection) -> None:
    """Make a returning connection look fresh; discard it if that is not possible.

    Done here, synchronously, rather than with the pool's ``reset`` callback: that
    callback moves every return to a background worker, and a connection still on
    its way back when the pool closes would be left open.
    """
    if conn.closed:
        return
    try:
        if conn.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
            conn.rollback()
        conn.execute("SELECT pg_advisory_unlock_all()")
    except psycopg.Error:
        conn.close()  # the pool discards a closed connection instead of reusing it


def _pool(dsn: str) -> ConnectionPool:
    with _lock:
        found = _pools.get(dsn)
        if found is None:
            found = ConnectionPool(
                dsn,
                min_size=0,
                max_size=MAX_SIZE,
                timeout=TIMEOUT,
                max_idle=MAX_IDLE,
                kwargs={"autocommit": True},
                check=ConnectionPool.check_connection,
                name="eki",
                open=True,
            )
            _pools[dsn] = found
        return found


@contextmanager
def connection(dsn: str) -> Iterator[psycopg.Connection]:
    """Borrow an autocommit connection; it returns to the pool when the block ends.

    A pool timeout raises ``psycopg_pool.PoolTimeout``, a ``psycopg.OperationalError``,
    so callers' existing database-unavailable handling still applies.
    """
    with _pool(dsn).connection() as conn:
        try:
            yield conn
        finally:
            _release(conn)


def close(dsn: str | None = None) -> None:
    """Close one pool, or every pool, and its connections."""
    with _lock:
        if dsn is None:
            targets = list(_pools.values())
            _pools.clear()
        else:
            found = _pools.pop(dsn, None)
            targets = [] if found is None else [found]
    for target in targets:
        target.close()


atexit.register(close)
