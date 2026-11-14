"""F13: durable metadata-only audit events and an append-only storage boundary."""

from typing import cast
from uuid import uuid4

import psycopg
import pytest

from eki.audit import AuditDetails, append_event, audit_context, list_events
from tests.interview_support import seed_run

pytestmark = pytest.mark.db


def test_audit_is_scoped_paginated_and_survives_run_deletion(conn: psycopg.Connection) -> None:
    run = seed_run(conn)
    correlation = uuid4()
    with audit_context(user_id="expert-subject", run_ids=(run,), correlation_id=correlation):
        for _ in range(3):
            append_event(conn, "tool", "completed", AuditDetails(operation="get_artifact"))
    first = list_events(conn, run, limit=2)
    assert len(first) == 2 and all(e["user_id"] == "expert-subject" for e in first)
    assert all(e["correlation_id"] == str(correlation) for e in first)
    assert len(list_events(conn, run, after=cast(int, first[-1]["id"]))) == 1
    conn.execute("DELETE FROM ingest_runs WHERE id = %s", (run,))
    assert len(list_events(conn, run)) == 3


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE audit_events SET user_id = 'changed'",
        "DELETE FROM audit_events",
        "TRUNCATE audit_events",
    ],
)
def test_audit_events_cannot_be_mutated(conn: psycopg.Connection, statement: str) -> None:
    append_event(conn, "tool", "started", AuditDetails(operation="get_artifact"))
    with pytest.raises(psycopg.Error), conn.transaction():
        conn.execute(statement)


def test_audit_details_refuse_free_text_payloads() -> None:
    with pytest.raises(ValueError):
        AuditDetails.model_validate({"operation": "get_artifact", "answer": "private"})
    with pytest.raises(ValueError):
        AuditDetails(operation="password=do-not-log")
