"""Write chunk embeddings to pgvector and search them within one ingest run."""

from dataclasses import dataclass
from uuid import UUID

import psycopg

from eki.ingest.chunking import chunk
from eki.ingest.embedding import Embedder, to_vector_literal
from eki.ingest.redaction import RedactedText


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
    vectors = embedder.embed([text for _, _, text in pending])
    with conn.transaction():
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
    return len(pending)


def search_chunks(
    conn: psycopg.Connection,
    run_id: UUID,
    query: RedactedText,
    embedder: Embedder,
    k: int = 5,
) -> list[ChunkHit]:
    """The ``k`` chunks of one run closest to the query, best first."""
    (vector,) = embedder.embed([query])
    literal = to_vector_literal(vector)
    with conn.transaction():
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
