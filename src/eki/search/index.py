"""Chunk approved sections and refresh only content visible to the current identity."""

import psycopg

from eki.access import VISIBLE_RUN, visible_params
from eki.auth import Principal
from eki.ingest.chunking import chunk
from eki.ingest.embedding import to_vector_literal
from eki.ingest.redaction import RedactedText, redact
from eki.runbook.models import SECTION_NAMES, RunbookEntry, SectionName
from eki.search.embedding import SearchEmbedder, checked_vectors


def refresh(
    conn: psycopg.Connection,
    principal: Principal,
    embedder: SearchEmbedder,
    domains: frozenset[str],
) -> int:
    rows = conn.execute(
        "SELECT e.id, e.content, e.version FROM runbook_entries e "  # noqa: S608
        "JOIN sessions s ON s.id = e.session_id "
        "JOIN interview_plans p ON p.id = s.plan_id "
        "JOIN ingest_runs r ON r.id = p.run_id WHERE e.status = 'approved' AND "
        + VISIBLE_RUN
        + " AND NOT EXISTS (SELECT 1 FROM runbook_index i WHERE i.entry_id = e.id "
        "AND i.version = e.version AND i.model = %s) ORDER BY e.id",
        (*visible_params(principal), embedder.name),
    ).fetchall()
    pending: list[tuple[str, SectionName, int, int, RedactedText, RedactedText]] = []
    for entry_id, content, version in rows:
        entry = RunbookEntry.model_validate(content)
        title = redact(entry.title, work_email_domains=domains).text
        for name in SECTION_NAMES:
            section = entry.section(name)
            text = "\n\n".join(c.text for c in section.claims)
            if section.open_question is not None:
                text += "\n\nOpen question: " + section.open_question
            clean = redact(text, work_email_domains=domains).text
            for ordinal, body in enumerate(chunk(clean)):
                pending.append((str(entry_id), name, ordinal, version, title, body))
    # Bounded batches avoid exceeding provider input-count limits on large runbooks.
    count = 0
    for start in range(0, len(pending), 64):
        batch = pending[start : start + 64]
        texts = [RedactedText(f"{title[:200]}\n{body}") for _, _, _, _, title, body in batch]
        vectors = checked_vectors(embedder.embed(texts), len(texts))
        with conn.transaction():
            for (entry_id, name, ordinal, version, title, body), vector in zip(
                batch, vectors, strict=True
            ):
                cursor = conn.execute(
                    "INSERT INTO runbook_index "  # noqa: S608 - fixed predicate; bound values
                    "(entry_id, section, ordinal, version, model, title, body, embedding) "
                    "SELECT e.id, %s, %s, e.version, %s, %s, %s, %s::vector FROM runbook_entries e "
                    "JOIN sessions s ON s.id = e.session_id "
                    "JOIN interview_plans p ON p.id = s.plan_id "
                    "JOIN ingest_runs r ON r.id = p.run_id WHERE e.id = %s AND e.version = %s "
                    "AND e.status = 'approved' AND "
                    + VISIBLE_RUN
                    + " ON CONFLICT (entry_id, section, ordinal, model) DO UPDATE SET "
                    "version = EXCLUDED.version, title = EXCLUDED.title, body = EXCLUDED.body, "
                    "embedding = EXCLUDED.embedding "
                    "WHERE runbook_index.version <= EXCLUDED.version",
                    (
                        name,
                        ordinal,
                        embedder.name,
                        title,
                        body,
                        to_vector_literal(vector),
                        entry_id,
                        version,
                        *visible_params(principal),
                    ),
                )
                count += cursor.rowcount
    return count
