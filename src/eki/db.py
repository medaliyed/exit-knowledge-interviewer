"""Postgres connection and a small migration runner (plain SQL files, no ORM)."""

from pathlib import Path

import psycopg
from psycopg import sql

from eki.config import Settings

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def connect(settings: Settings) -> psycopg.Connection:
    """Open a connection. Transactions are explicit: callers commit."""
    return psycopg.connect(settings.database_url.get_secret_value())


def pending_migrations(conn: psycopg.Connection, directory: Path | None = None) -> list[str]:
    """Names of the migration files not yet applied, in name order. Changes nothing."""
    files = sorted(path.name for path in (directory or MIGRATIONS_DIR).glob("*.sql"))
    with conn.transaction():
        # ``migrate`` creates the table in the current schema; a search path may also
        # reach another schema's table, which says nothing about this one.
        row = conn.execute(
            "SELECT to_regclass(format('%I.schema_migrations', current_schema()))::text"
        ).fetchone()
        if row is None or row[0] is None:
            return files
        table = sql.SQL("SELECT name FROM {}").format(sql.SQL(row[0]))
        applied = {r[0] for r in conn.execute(table).fetchall()}
    return [name for name in files if name not in applied]


def pending_warning(conn: psycopg.Connection) -> str | None:
    """A one-line warning when migrations are pending, for the CLI and the API to show."""
    pending = pending_migrations(conn)
    if not pending:
        return None
    count = f"{len(pending)} database migration{' is' if len(pending) == 1 else 's are'}"
    return f"{count} pending ({', '.join(pending)}); run: eki migrate"


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
