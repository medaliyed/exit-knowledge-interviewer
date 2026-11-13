"""Helpers shared by the MCP server and ingest tests."""

import hashlib
import json
import shutil
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from psycopg import sql

from mcp_servers.common.source import Scope

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = REPO_ROOT / "tests" / "fixtures"
CORPUS = FIXTURES / "corpus"
EXPERT_NAME = "Nadia Ferrand"
EXPERT_EMAIL = "nadia.ferrand@example.com"
WORK_DOMAINS = frozenset({"example.com"})
FULL_SPAN = Scope(
    expert_name=EXPERT_NAME,
    expert_email=EXPERT_EMAIL,
    since=datetime(2024, 11, 1, tzinfo=UTC),
    until=datetime(2026, 10, 31, 23, 59, 59, tzinfo=UTC),
    work_email_domains=WORK_DOMAINS,
)
FAKE_CREDENTIALS = (
    "FAKE-APIKEY-7d1e4c90b2a853f6",
    "FAKE-PASSWORD-ledger-Qm7x2Lp9",
    "FAKE-TOKEN-3b9f6e2a8c4d1075",
)


def raw_commits() -> list[dict[str, Any]]:
    lines = (CORPUS / "git" / "commits.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line]


def raw_pull_requests() -> list[dict[str, Any]]:
    lines = (CORPUS / "git" / "pull_requests.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line]


def raw_tickets() -> list[dict[str, Any]]:
    return [
        json.loads(p.read_text(encoding="utf-8"))
        for p in sorted((CORPUS / "tickets").glob("*.json"))
    ]


def tree_hashes(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def read_only_copy(destination: Path, *, without: tuple[str, ...] = ()) -> Path:
    """Copy the corpus, drop some top-level directories, and remove every write permission."""
    target = destination / "corpus"
    shutil.copytree(CORPUS, target)
    for name in without:
        shutil.rmtree(target / name)
    for path in [target, *target.rglob("*")]:
        mode = path.stat().st_mode
        path.chmod(mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
    return target


def make_writable(root: Path) -> None:
    for path in [root, *root.rglob("*")]:
        path.chmod(path.stat().st_mode | stat.S_IWUSR)


def tables_containing(conn: Any, schema: str, needle: str) -> list[str]:
    """Every ``table.column`` of a schema that contains the needle, in text or binary columns."""
    columns = conn.execute(
        "SELECT table_name, column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = %s ORDER BY table_name, ordinal_position",
        (schema,),
    ).fetchall()
    found = []
    for table, column, data_type in columns:
        target = sql.SQL("{}.{}").format(sql.Identifier(schema), sql.Identifier(table))
        if data_type == "bytea":
            condition = sql.SQL("position(convert_to(%s, 'UTF8') in {}) > 0")
        else:
            condition = sql.SQL("position(%s in {}::text) > 0")
        query = sql.SQL("SELECT count(*) FROM {} WHERE ").format(target) + condition.format(
            sql.Identifier(column)
        )
        if conn.execute(query, (needle,)).fetchone()[0]:
            found.append(f"{table}.{column}")
    return found
