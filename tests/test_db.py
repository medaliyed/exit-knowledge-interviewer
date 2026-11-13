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
}


def table_names(empty_conn: psycopg.Connection) -> set[str]:
    rows = empty_conn.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema()"
    ).fetchall()
    return {row[0] for row in rows}


def test_migrate_creates_schema(empty_conn: psycopg.Connection) -> None:
    applied = migrate(empty_conn)
    assert applied == ["0001_init.sql"]
    assert table_names(empty_conn) == TABLES


def test_migrate_is_idempotent(empty_conn: psycopg.Connection) -> None:
    migrate(empty_conn)
    assert migrate(empty_conn) == []
    assert empty_conn.execute("SELECT count(*) FROM schema_migrations").fetchone() == (1,)


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
