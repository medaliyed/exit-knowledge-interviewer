"""Prepare and atomically publish complete runbook indexes, called only by workers."""

from dataclasses import dataclass

import psycopg

from eki.ingest.chunking import chunk
from eki.ingest.embedding import to_vector_literal
from eki.ingest.redaction import RedactedText, redact
from eki.runbook.models import SECTION_NAMES, RunbookEntry, SectionName
from eki.search.embedding import SearchEmbedder, checked_vectors


@dataclass(frozen=True)
class Prepared:
    title: RedactedText
    chunks: list[tuple[SectionName, int, RedactedText]]
    vectors: list[list[float]]


def prepare(entry: RunbookEntry, embedder: SearchEmbedder, domains: frozenset[str]) -> Prepared:
    title = redact(entry.title, work_email_domains=domains).text
    pending: list[tuple[SectionName, int, RedactedText]] = []
    for name in SECTION_NAMES:
        section = entry.section(name)
        text = "\n\n".join(c.text for c in section.claims)
        if section.open_question is not None:
            text += "\n\nOpen question: " + section.open_question
        clean = redact(text, work_email_domains=domains).text
        pending.extend((name, ordinal, body) for ordinal, body in enumerate(chunk(clean)))
    vectors: list[list[float]] = []
    for start in range(0, len(pending), 64):
        texts = [
            RedactedText(f"{title[:200]}\n{body}") for _, _, body in pending[start : start + 64]
        ]
        vectors.extend(checked_vectors(embedder.embed(texts), len(texts)))
    return Prepared(title, pending, vectors)


def publish(
    conn: psycopg.Connection, entry_id: str, version: int, model: str, prepared: Prepared
) -> None:
    """The worker holds run/entry locks and has rechecked live approval and version."""
    conn.execute("DELETE FROM runbook_index WHERE entry_id=%s AND model=%s", (entry_id, model))
    with conn.cursor() as cursor:
        cursor.executemany(
            "INSERT INTO runbook_index "
            "(entry_id,section,ordinal,version,model,title,body,embedding) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s::vector)",
            [
                (
                    entry_id,
                    name,
                    ordinal,
                    version,
                    model,
                    prepared.title,
                    body,
                    to_vector_literal(v),
                )
                for (name, ordinal, body), v in zip(prepared.chunks, prepared.vectors, strict=True)
            ],
        )
