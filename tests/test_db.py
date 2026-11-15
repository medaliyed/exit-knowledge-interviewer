"""Deliverable e: the schema migrates cleanly, idempotently and atomically. Needs Postgres."""

import uuid
from pathlib import Path

import psycopg
import pytest

from eki.db import migrate

pytestmark = pytest.mark.db

TABLES = {
    "schema_migrations",
    "ingest_runs",
    "artifacts",
    "activity",
    "chunks",
    "topics",
    "topic_artifacts",
    "gap_scores",
    "interview_plans",
    "plan_topics",
    "questions",
    "sessions",
    "turns",
    "notes",
    "open_items",
    "runbook_entries",
    "entry_decisions",
    "accounts",
    "auth_sessions",
    "auth_links",
    "auth_throttle",
    "run_readers",
    "runbook_index",
    "audit_events",
    "message_receipts",
    "interview_replies",
    "plan_generations",
    "generation_topics",
    "handovers",
    "entry_flags",
    "flag_throttle",
    "follow_up_gaps",
    "follow_up_throttle",
}
MIGRATIONS = [
    "0001_init.sql",
    "0002_interview.sql",
    "0003_runbook.sql",
    "0004_access.sql",
    "0005_runbook_search.sql",
    "0006_audit.sql",
    "0007_mail.sql",
    "0008_entry_edited.sql",
    "0009_delivery.sql",
    "0010_artifact_model.sql",
    "0011_generation.sql",
    "0012_accounts.sql",
    "0013_maintenance.sql",
    "0014_feedback_decisions.sql",
    "0015_maintenance_index.sql",
    "0016_follow_up_gaps.sql",
]


def table_names(empty_conn: psycopg.Connection) -> set[str]:
    rows = empty_conn.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema()"
    ).fetchall()
    return {row[0] for row in rows}


def test_migrate_creates_schema(empty_conn: psycopg.Connection) -> None:
    applied = migrate(empty_conn)
    assert applied == MIGRATIONS
    assert table_names(empty_conn) == TABLES


def test_migrate_is_idempotent(empty_conn: psycopg.Connection) -> None:
    migrate(empty_conn)
    assert migrate(empty_conn) == []
    assert empty_conn.execute("SELECT count(*) FROM schema_migrations").fetchone() == (
        len(MIGRATIONS),
    )


def insert_run_and_artifact(empty_conn: psycopg.Connection) -> uuid.UUID:
    run_id = uuid.uuid4()
    empty_conn.execute(
        "INSERT INTO ingest_runs (id, expert_name, expert_email, since, until, status) "
        "VALUES (%s, 'N', 'n@example.com', now(), now(), 'running')",
        (run_id,),
    )
    empty_conn.execute(
        "INSERT INTO artifacts (run_id, id, source, kind, occurred_at, title, body, expert_role, "
        "metadata, redaction_counts) VALUES (%s, 'A1', 'git', 'commit', now(), 't', 'b', 'author', "
        "'{}', '{}')",
        (run_id,),
    )
    return run_id


def test_vector_column_accepts_256_dimensions_only(empty_conn: psycopg.Connection) -> None:
    migrate(empty_conn)
    run_id = insert_run_and_artifact(empty_conn)
    good = "[" + ",".join(["0.5"] * 256) + "]"
    empty_conn.execute(
        "INSERT INTO chunks (run_id, artifact_id, ordinal, text, embedding) "
        "VALUES (%s, 'A1', 0, 'x', %s::vector)",
        (run_id, good),
    )
    with pytest.raises(psycopg.Error):
        empty_conn.execute(
            "INSERT INTO chunks (run_id, artifact_id, ordinal, text, embedding) "
            "VALUES (%s, 'A1', 1, 'x', '[1,2,3]'::vector)",
            (run_id,),
        )


def test_deleting_a_run_removes_its_rows(empty_conn: psycopg.Connection) -> None:
    migrate(empty_conn)
    run_id = insert_run_and_artifact(empty_conn)
    empty_conn.execute("DELETE FROM ingest_runs WHERE id = %s", (run_id,))
    assert empty_conn.execute("SELECT count(*) FROM artifacts").fetchone() == (0,)


def test_status_is_restricted(empty_conn: psycopg.Connection) -> None:
    migrate(empty_conn)
    with pytest.raises(psycopg.Error):
        empty_conn.execute(
            "INSERT INTO ingest_runs (id, expert_name, expert_email, since, until, status) "
            "VALUES (%s, 'N', 'n@x', now(), now(), 'banana')",
            (uuid.uuid4(),),
        )


def test_failed_migration_rolls_back(empty_conn: psycopg.Connection, tmp_path: Path) -> None:
    (tmp_path / "0001_ok.sql").write_text("CREATE TABLE first_table (id int);", encoding="utf-8")
    (tmp_path / "0002_bad.sql").write_text(
        "CREATE TABLE second_table (id int); SELECT 1 / 0;", encoding="utf-8"
    )
    with pytest.raises(psycopg.Error):
        migrate(empty_conn, directory=tmp_path)
    empty_conn.rollback()
    names = table_names(empty_conn)
    assert "first_table" in names
    assert "second_table" not in names
    recorded = {
        row[0] for row in empty_conn.execute("SELECT name FROM schema_migrations").fetchall()
    }
    assert recorded == {"0001_ok.sql"}


def test_existing_edited_drafts_are_marked_when_the_flag_is_added(
    empty_conn: psycopg.Connection, tmp_path: Path
) -> None:
    """Migration 0008 must protect edits made before the flag existed."""
    import shutil

    from eki.db import MIGRATIONS_DIR

    before = tmp_path / "before"
    before.mkdir()
    for name in MIGRATIONS[:7]:
        shutil.copy(MIGRATIONS_DIR / name, before / name)
    migrate(empty_conn, before)
    run_id, plan_id, session_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    empty_conn.execute(
        "INSERT INTO ingest_runs (id, expert_name, expert_email, since, until, status) "
        "VALUES (%s, 'N', 'n@example.com', now(), now(), 'completed')",
        (run_id,),
    )
    empty_conn.execute(
        "INSERT INTO interview_plans (id, run_id, status) VALUES (%s, %s, 'draft')",
        (plan_id, run_id),
    )
    empty_conn.execute(
        "INSERT INTO sessions (id, plan_id, status) VALUES (%s, %s, 'finished')",
        (session_id, plan_id),
    )
    entries = {}
    for topic, status, action in (
        ("edited-draft", "draft", "edit"),
        ("plain-draft", "draft", None),
        ("rejected-then-untouched", "rejected", "reject"),
    ):
        entry_id = uuid.uuid4()
        entries[topic] = entry_id
        empty_conn.execute(
            "INSERT INTO runbook_entries (id, session_id, topic_id, title, status, content, "
            "rejection_reason) VALUES (%s, %s, %s, 't', %s, '{}', %s)",
            (entry_id, session_id, topic, status, "no" if status == "rejected" else None),
        )
        if action:
            empty_conn.execute(
                "INSERT INTO entry_decisions (entry_id, action, who, at) "
                "VALUES (%s, %s, 'N', now())",
                (entry_id, action),
            )
    empty_conn.commit()
    assert migrate(empty_conn) == MIGRATIONS[7:]
    rows = empty_conn.execute("SELECT topic_id, edited FROM runbook_entries").fetchall()
    flags: dict[str, bool] = dict(rows)
    assert flags == {
        "edited-draft": True,
        "plain-draft": False,
        "rejected-then-untouched": False,
    }
