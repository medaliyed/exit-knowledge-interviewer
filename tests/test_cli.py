"""Deliverable j: the eki command line runs the phase 1 pipeline end to end. Needs Postgres."""

import json
import uuid
from collections.abc import Iterator
from urllib.parse import quote

import pytest
from psycopg import sql

from eki.cli import main
from eki.config import get_settings
from eki.db import connect
from tests.support import CORPUS, EXPERT_EMAIL, EXPERT_NAME, FAKE_CREDENTIALS

pytestmark = pytest.mark.db

RANGE = ["--since", "2024-11-01", "--until", "2026-10-31"]


@pytest.fixture
def scratch_database(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Point the CLI at a throwaway schema through the connection URL, then drop it."""
    get_settings.cache_clear()
    base_url = get_settings().database_url.get_secret_value()
    schema = f"test_{uuid.uuid4().hex[:12]}"
    admin = connect(get_settings())
    admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    admin.commit()
    separator = "&" if "?" in base_url else "?"
    options = quote(f"-csearch_path={schema},public")
    monkeypatch.setenv("EKI_DATABASE_URL", f"{base_url}{separator}options={options}")
    monkeypatch.setenv("EKI_EXPERT_NAME", EXPERT_NAME)
    monkeypatch.setenv("EKI_EXPERT_EMAIL", EXPERT_EMAIL)
    monkeypatch.setenv("EKI_WORK_EMAIL_DOMAINS", "example.com")
    monkeypatch.setenv("EKI_SOURCE_DIR", str(CORPUS))
    get_settings.cache_clear()
    try:
        yield
    finally:
        admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
        admin.commit()
        admin.close()
        get_settings.cache_clear()


def ingest(capsys: pytest.CaptureFixture[str]) -> str:
    assert main(["migrate"]) == 0
    capsys.readouterr()
    assert main(["ingest", *RANGE]) == 0
    run_id = capsys.readouterr().out.strip().splitlines()[-1]
    uuid.UUID(run_id)
    return run_id


def test_cli_end_to_end(scratch_database: None, capsys: pytest.CaptureFixture[str]) -> None:
    run_id = ingest(capsys)

    assert main(["map", "--run", run_id]) == 0
    assert "topics" in capsys.readouterr().out

    assert main(["gaps", "--run", run_id, "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert len(rows) > 20
    scores = [row["score"] for row in rows]
    assert scores == sorted(scores, reverse=True)
    assert set(rows[0]) >= {
        "rank",
        "topic_id",
        "kind",
        "system",
        "label",
        "score",
        "exclusivity",
        "doc_gap",
        "recency",
        "criticality",
        "doc_status",
        "linked_documents",
        "explanation",
        "artifact_count",
    }
    assert rows[0]["rank"] == 1
    top_ten = {row["topic_id"] for row in rows[:10]}
    assert "system:report-export:report-export" in top_ten

    assert main(["gaps", "--run", run_id, "--limit", "5"]) == 0
    table = capsys.readouterr().out
    assert len([line for line in table.splitlines() if line[:4].strip().isdigit()]) == 5
    assert "report-export" in table
    for secret in FAKE_CREDENTIALS:
        assert secret not in table
        assert secret not in json.dumps(rows)


def test_ingest_indexes_chunks(scratch_database: None, capsys: pytest.CaptureFixture[str]) -> None:
    run_id = ingest(capsys)
    with connect(get_settings()) as conn:
        chunks = conn.execute("SELECT count(*) FROM chunks WHERE run_id = %s", (run_id,)).fetchone()
        artifacts = conn.execute(
            "SELECT count(*) FROM artifacts WHERE run_id = %s", (run_id,)
        ).fetchone()
    assert chunks is not None and artifacts is not None
    assert chunks[0] >= artifacts[0] > 200


def test_migrate_twice_is_fine(scratch_database: None, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["migrate"]) == 0
    assert "0001_init.sql" in capsys.readouterr().out
    assert main(["migrate"]) == 0
    assert "up to date" in capsys.readouterr().out.lower()


def test_gaps_for_unknown_run_exits_non_zero(
    scratch_database: None, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["migrate"]) == 0
    assert main(["gaps", "--run", str(uuid.uuid4())]) == 1
    assert "no completed run" in capsys.readouterr().err.lower()


def test_map_for_unknown_run_exits_non_zero(
    scratch_database: None, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["migrate"]) == 0
    assert main(["map", "--run", str(uuid.uuid4())]) == 1
    assert "no completed run" in capsys.readouterr().err.lower()


def test_gaps_before_map_says_what_to_do(
    scratch_database: None, capsys: pytest.CaptureFixture[str]
) -> None:
    run_id = ingest(capsys)
    assert main(["gaps", "--run", run_id]) == 1
    assert "eki map" in capsys.readouterr().err


def test_ingest_without_an_expert_is_refused(
    scratch_database: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("EKI_EXPERT_NAME", "")
    monkeypatch.setenv("EKI_EXPERT_EMAIL", "")
    get_settings.cache_clear()
    assert main(["ingest", *RANGE]) == 2
    assert "expert" in capsys.readouterr().err.lower()


def test_a_malformed_run_id_is_a_usage_error(scratch_database: None) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["gaps", "--run", "not-a-uuid"])
    assert excinfo.value.code == 2


def test_a_reversed_date_range_is_refused(
    scratch_database: None, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["ingest", "--since", "2026-01-01", "--until", "2025-01-01"]) == 2
    assert "before" in capsys.readouterr().err.lower()
