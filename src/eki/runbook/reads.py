"""Consistent review reads, with batched metadata and explicit source lineage."""

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

import psycopg

from eki.errors import NotFoundError
from eki.followup import proposals
from eki.interview import store as interviews
from eki.runbook import faithfulness, maintenance, store


@dataclass(frozen=True)
class EntrySummary:
    stored: store.StoredEntry
    unsupported: tuple[faithfulness.Unsupported, ...]
    metadata: dict[str, Any]


@dataclass(frozen=True)
class EntryDetail(EntrySummary):
    decisions: tuple[store.Decision, ...]


@contextmanager
def snapshot(conn: psycopg.Connection) -> Iterator[None]:
    """A short read-only snapshot; settings end with the transaction, not the connection."""
    with conn.transaction():
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        yield


def _checks(
    conn: psycopg.Connection, session_id: UUID, entries: Sequence[store.StoredEntry]
) -> dict[str, tuple[faithfulness.Unsupported, ...]]:
    if not entries:
        return {}
    home = interviews.load_turns(conn, session_id)
    linked = proposals.linked_sessions_many(
        conn, {stored.entry.id: stored.entry.topic_id for stored in entries}
    )
    # Several entries can cite one follow-up. Load its transcript once, but pass
    # it only to entries explicitly linked by a proposal for their own topic.
    sources = {
        session: interviews.load_turns(conn, session)
        for session in {s for sessions in linked.values() for s in sessions}
        if session != session_id
    }
    sources[session_id] = home
    return {
        stored.entry.id: faithfulness.check_entry(
            stored.entry,
            home,
            session_id=str(session_id),
            linked={str(s): sources[s] for s in linked.get(stored.entry.id, ())},
        )
        for stored in entries
    }


def summaries(
    conn: psycopg.Connection, session_id: UUID, now: datetime
) -> tuple[EntrySummary, ...]:
    with snapshot(conn):
        if interviews.load_session(conn, session_id) is None:
            raise NotFoundError(f"no session {session_id}")
        entries = store.list_entries(conn, session_id)
        if not entries:
            return ()
        metadata = maintenance.infos(conn, [s.entry.id for s in entries], now)
        unsupported = _checks(conn, session_id, entries)
        return tuple(
            EntrySummary(s, unsupported[s.entry.id], metadata[s.entry.id]) for s in entries
        )


def detail(conn: psycopg.Connection, entry_id: UUID, now: datetime) -> EntryDetail:
    with snapshot(conn):
        stored = store.load_entry(conn, str(entry_id))
        if stored is None:
            raise NotFoundError(f"no entry {entry_id}")
        unsupported = _checks(conn, stored.session_id, (stored,))[stored.entry.id]
        return EntryDetail(
            stored,
            unsupported,
            maintenance.info(conn, entry_id, now),
            store.decisions(conn, str(entry_id)),
        )
