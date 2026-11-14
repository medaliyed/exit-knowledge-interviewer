"""Write chunk embeddings to pgvector and search them within one ingest run."""

from dataclasses import dataclass
from uuid import UUID

import psycopg

from eki.ingest.chunking import chunk
from eki.ingest.embedding import Embedder, to_vector_literal
from eki.ingest.redaction import RedactedText
from eki.llm import ModelError
from eki.search.embedding import checked_vectors


@dataclass(frozen=True)
class ChunkHit:
    artifact_id: str
    ordinal: int
    text: RedactedText
    score: float
    """Cosine similarity, 1.0 for identical direction."""


def _stored(value: str) -> RedactedText:
    # Only ``store`` writes artifacts, and it accepts nothing but redacted snapshots.
    return RedactedText(value)


def index_run(conn: psycopg.Connection, run_id: UUID, embedder: Embedder) -> int:
    """Chunk and embed every artifact of a run. Idempotent. Returns the chunks written."""
    rows = conn.execute(
        'SELECT id, title, body FROM artifacts WHERE run_id = %s ORDER BY id COLLATE "C"',
        (run_id,),
    ).fetchall()
    pending: list[tuple[str, int, RedactedText]] = []
    for artifact_id, title, body in rows:
        full = RedactedText(f"{_stored(title)}\n\n{_stored(body)}")
        pending += [(artifact_id, ordinal, text) for ordinal, text in enumerate(chunk(full))]
    vectors: list[list[float]] = []
    for start in range(0, len(pending), 64):
        batch = [text for _, _, text in pending[start : start + 64]]
        vectors.extend(checked_vectors(embedder.embed(batch), len(batch)))
    with conn.transaction():
        conn.execute("SELECT 1 FROM ingest_runs WHERE id=%s FOR UPDATE", (run_id,))
        conn.execute("DELETE FROM chunks WHERE run_id = %s", (run_id,))
        with conn.cursor() as cursor:
            cursor.executemany(
                "INSERT INTO chunks (run_id, artifact_id, ordinal, text, embedding) "
                "VALUES (%s, %s, %s, %s, %s::vector)",
                [
                    (run_id, artifact_id, ordinal, text, to_vector_literal(vector))
                    for (artifact_id, ordinal, text), vector in zip(pending, vectors, strict=True)
                ],
            )
        conn.execute(
            "UPDATE ingest_runs SET artifact_embedding_model=%s WHERE id=%s",
            (embedder.name, run_id),
        )
    return len(pending)


def search_chunks(
    conn: psycopg.Connection,
    run_id: UUID,
    query: RedactedText,
    embedder: Embedder,
    k: int = 5,
) -> list[ChunkHit]:
    """The ``k`` chunks of one run closest to the query, best first."""
    model = recorded_model(conn, run_id)
    if model is None:
        return []
    if embedder.name != model:
        raise ModelError(
            "artifact query model differs from its index; reindex or use the recorded model"
        )
    (vector,) = checked_vectors(embedder.embed([query]), 1)
    literal = to_vector_literal(vector)
    with conn.transaction():
        row = conn.execute(
            "SELECT artifact_embedding_model FROM ingest_runs WHERE id=%s FOR SHARE", (run_id,)
        ).fetchone()
        if row is None or row[0] != model:
            raise ModelError("artifact index model changed during query; retry")
        # The index is approximate and the run filter is applied after it; iterative scan
        # keeps scanning until enough rows from this run have been found.
        conn.execute("SET LOCAL hnsw.iterative_scan = relaxed_order")
        rows = conn.execute(
            "SELECT artifact_id, ordinal, text, 1 - (embedding <=> %s::vector) AS score "
            "FROM chunks WHERE run_id = %s ORDER BY embedding <=> %s::vector LIMIT %s",
            (literal, run_id, literal, k),
        ).fetchall()
    return [
        ChunkHit(artifact_id=r[0], ordinal=r[1], text=_stored(r[2]), score=float(r[3]))
        for r in rows
    ]


def recorded_model(conn: psycopg.Connection, run_id: UUID) -> str | None:
    row = conn.execute(
        "SELECT artifact_embedding_model FROM ingest_runs WHERE id=%s", (run_id,)
    ).fetchone()
    return None if row is None else str(row[0])
