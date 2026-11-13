"""Postgres connection and a small migration runner (plain SQL files, no ORM)."""

from pathlib import Path

import psycopg

from eki.config import Settings

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def connect(settings: Settings) -> psycopg.Connection:
    """Open a connection. Transactions are explicit: callers commit."""
    return psycopg.connect(settings.database_url.get_secret_value())


def migrate(conn: psycopg.Connection, directory: Path = MIGRATIONS_DIR) -> list[str]:
    """Apply every migration file not yet recorded, in name order, one transaction per file.

    Returns the names it applied. A file that fails leaves no trace of itself, and the files
    applied before it stay applied.
    """
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
    )
    conn.commit()
    applied = {row[0] for row in conn.execute("SELECT name FROM schema_migrations").fetchall()}
    newly_applied: list[str] = []
    for path in sorted(directory.glob("*.sql")):
        if path.name in applied:
            continue
        with conn.transaction():
            conn.execute(path.read_text(encoding="utf-8"))
            conn.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (path.name,))
        conn.commit()
        newly_applied.append(path.name)
    return newly_applied
