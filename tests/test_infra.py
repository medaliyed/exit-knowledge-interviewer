"""Deliverable b, live checks. Needs `docker compose up -d`; run with `uv run pytest -m infra`."""

import subprocess
import urllib.request
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
LANGFUSE_HEALTH_URL = "http://localhost:3000/api/public/health"

pytestmark = pytest.mark.infra


def psql(sql: str) -> str:
    """Run one SQL statement in the eki database inside the postgres container."""
    result = subprocess.run(
        [
            "docker",
            "compose",
            "exec",
            "-T",
            "postgres",
            "sh",
            "-c",
            'psql -U "$POSTGRES_USER" -d eki -tAc "$0"',
            sql,
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_pgvector_extension_installed() -> None:
    assert psql("SELECT 1 FROM pg_extension WHERE extname = 'vector'") == "1"


def test_postgres_major_version_is_16() -> None:
    assert psql("SHOW server_version").split(".")[0] == "16"


def test_langfuse_database_exists() -> None:
    assert psql("SELECT 1 FROM pg_database WHERE datname = 'langfuse'") == "1"


def test_langfuse_healthy() -> None:
    with urllib.request.urlopen(LANGFUSE_HEALTH_URL, timeout=10) as response:
        assert response.status == 200
