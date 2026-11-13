"""Deliverable g: chunks are written to pgvector and can be searched within one run."""

from datetime import UTC, datetime

import psycopg
import pytest

from eki.ingest.embedding import HashingEmbedder
from eki.ingest.index import index_run, search_chunks
from eki.ingest.models import Artifact, Snapshot
from eki.ingest.pipeline import store
from eki.ingest.redaction import RedactedText

pytestmark = [pytest.mark.db, pytest.mark.anyio]


async def test_index_writes_one_row_per_chunk(conn: psycopg.Connection, snapshot: Snapshot) -> None:
    run_id = store(conn, snapshot)
    written = index_run(conn, run_id, HashingEmbedder())
    stored = conn.execute("SELECT count(*) FROM chunks WHERE run_id = %s", (run_id,)).fetchone()
    assert stored == (written,)
    assert written >= len(snapshot.artifacts)
    covered = conn.execute(
        "SELECT count(DISTINCT artifact_id) FROM chunks WHERE run_id = %s", (run_id,)
    ).fetchone()
    assert covered == (len(snapshot.artifacts),)


async def test_indexing_twice_does_not_duplicate(
    conn: psycopg.Connection, snapshot: Snapshot
) -> None:
    run_id = store(conn, snapshot)
    first = index_run(conn, run_id, HashingEmbedder())
    second = index_run(conn, run_id, HashingEmbedder())
    assert first == second
    assert conn.execute("SELECT count(*) FROM chunks").fetchone() == (first,)


async def test_search_finds_the_certificate_renewal_ticket(
    conn: psycopg.Connection, snapshot: Snapshot
) -> None:
    run_id = store(conn, snapshot)
    embedder = HashingEmbedder()
    index_run(conn, run_id, embedder)
    hits = search_chunks(conn, run_id, RedactedText("gateway certificate renewal"), embedder, k=3)
    assert len(hits) == 3
    by_id = {a.id: a for a in snapshot.artifacts}
    assert any(
        "certificate renewal" in by_id[hit.artifact_id].title.lower()
        or by_id[hit.artifact_id].id == "WIKI-gateway-cert-renewal"
        for hit in hits
    )
    assert hits[0].score >= hits[-1].score


async def test_search_finds_the_stale_replica_incident(
    conn: psycopg.Connection, snapshot: Snapshot
) -> None:
    run_id = store(conn, snapshot)
    embedder = HashingEmbedder()
    index_run(conn, run_id, embedder)
    query = RedactedText("restarted the ledger-db replicas then re-ran billing-batch")
    hits = search_chunks(conn, run_id, query, embedder, k=5)
    titles = {next(a for a in snapshot.artifacts if a.id == h.artifact_id).title for h in hits}
    assert any("billing-batch run failed" in title for title in titles)


async def test_search_is_limited_to_one_run(conn: psycopg.Connection, snapshot: Snapshot) -> None:
    embedder = HashingEmbedder()
    extra = Artifact(
        id="ZEBRA-1",
        source="git",
        kind="document",
        system=None,
        occurred_at=datetime(2025, 1, 1, tzinfo=UTC),
        title=RedactedText("zebra quagga okapi"),
        body=RedactedText("zebra quagga okapi savanna"),
        expert_role="none",
        metadata={},
        redaction_counts={},
    )
    with_extra = snapshot.model_copy(update={"artifacts": (*snapshot.artifacts, extra)})
    first, second = store(conn, snapshot), store(conn, with_extra)
    index_run(conn, first, embedder)
    index_run(conn, second, embedder)
    query = RedactedText("zebra quagga okapi")
    in_first = search_chunks(conn, first, query, embedder, k=10)
    in_second = search_chunks(conn, second, query, embedder, k=10)
    assert len(in_first) == 10
    assert "ZEBRA-1" not in {h.artifact_id for h in in_first}
    assert in_second[0].artifact_id == "ZEBRA-1"


async def test_search_with_no_matching_run_is_empty(conn: psycopg.Connection) -> None:
    import uuid

    hits = search_chunks(conn, uuid.uuid4(), RedactedText("anything"), HashingEmbedder(), k=5)
    assert hits == []
