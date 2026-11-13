"""P3-a: validated storage, uniqueness, history and cascading deletion."""

from datetime import UTC, datetime
from uuid import uuid4

import psycopg
import pytest

from eki.ingest.redaction import RedactedText
from eki.runbook import store
from eki.runbook.models import EntryStatus, RunbookEntry
from tests.interview_support import seed_run
from tests.test_interview_store import open_session

pytestmark = pytest.mark.db


def test_round_trip_history_and_cascade(conn: psycopg.Connection) -> None:
    run_id = seed_run(conn)
    session = open_session(conn, run_id)
    entry = RunbookEntry(id=str(uuid4()), topic_id="a-topic", title="Recovery")
    stored = store.save_entry(conn, session, entry)
    assert store.load_entry(conn, entry.id) == stored
    assert stored.version == 1
    assert store.save_entry(conn, session, entry).version == 2
    now = datetime.now(UTC)
    for action in ("edit", "approve"):
        store.add_decision(conn, entry.id, action, RedactedText("Nadia"), now)
    assert [d.action for d in store.decisions(conn, entry.id)] == ["edit", "approve"]
    with pytest.raises(psycopg.errors.UniqueViolation), conn.transaction():
        store.save_entry(conn, session, entry.model_copy(update={"id": str(uuid4())}))
    conn.execute("DELETE FROM ingest_runs WHERE id = %s", (run_id,))
    assert store.list_entries(conn, session) == ()
    assert store.decisions(conn, entry.id) == ()


def test_store_revalidates_even_a_constructed_approved_entry(conn: psycopg.Connection) -> None:
    session = open_session(conn, seed_run(conn))
    entry = RunbookEntry.model_construct(
        id=str(uuid4()), topic_id="a-topic", title="Bad", status=EntryStatus.APPROVED
    )
    with pytest.raises(ValueError):
        store.save_entry(conn, session, entry)
    assert store.list_entries(conn, session) == ()
