"""Durable per-subject submission limits shared by every API worker."""

from datetime import datetime, timedelta
from typing import Literal

import psycopg
from psycopg import sql

from eki.accounts import Throttled

Table = Literal["flag_throttle", "follow_up_throttle"]


def take(
    conn: psycopg.Connection,
    table: Table,
    subject: str,
    now: datetime,
    *,
    limit: int = 10,
    window: timedelta = timedelta(minutes=1),
) -> None:
    """Spend one submission inside the caller's transaction, or raise ``Throttled``.

    The row lock serialises concurrent workers; a rolled-back transaction spends nothing.
    """
    name = sql.Identifier(table)
    conn.execute(
        sql.SQL(
            "INSERT INTO {}(subject,window_at,count) VALUES(%s,%s,0) "
            "ON CONFLICT(subject) DO NOTHING"
        ).format(name),
        (subject, now),
    )
    row = conn.execute(
        sql.SQL("SELECT window_at,count FROM {} WHERE subject=%s FOR UPDATE").format(name),
        (subject,),
    ).fetchone()
    if row is None:
        raise RuntimeError("missing throttle row")
    expired = now - row[0] >= window
    if not expired and row[1] >= limit:
        raise Throttled(max(1, int(window.total_seconds() - (now - row[0]).total_seconds())))
    conn.execute(
        sql.SQL("UPDATE {} SET window_at=%s,count=%s WHERE subject=%s").format(name),
        (now if expired else row[0], 1 if expired else row[1] + 1, subject),
    )
