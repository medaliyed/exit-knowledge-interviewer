"""Refresh complete approved entries; provider failures never leave partial indexes."""

from datetime import date

import psycopg
from psycopg import sql

from eki.access import VISIBLE_RUN, visible_params
from eki.audit import audit_context
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
    *,
    review_after: date | None = None,
) -> int:
    rows = conn.execute(
        sql.SQL(
            "SELECT e.id, e.content, e.version, p.run_id FROM runbook_entries e "
            "JOIN sessions s ON s.id = e.session_id "
            "JOIN interview_plans p ON p.id = s.plan_id "
            "JOIN ingest_runs r ON r.id = p.run_id WHERE e.status = 'approved' AND "
            "(%s::date IS NULL OR e.review_by >= %s) AND "
        )
        + sql.SQL(VISIBLE_RUN)
        + sql.SQL(
            " AND NOT EXISTS (SELECT 1 FROM runbook_index i WHERE i.entry_id = e.id "
            "AND i.version = e.version AND i.model = %s) ORDER BY e.id"
        ),
        (review_after, review_after, *visible_params(principal), embedder.name),
    ).fetchall()
    count = 0
    for entry_id, content, version, run_id in rows:
        entry = RunbookEntry.model_validate(content)
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
        # All provider work happens before the transaction and in bounded batches.
        # A failure in any batch leaves this entry completely unindexed and retryable.
        for start in range(0, len(pending), 64):
            texts = [
                RedactedText(f"{title[:200]}\n{body}") for _, _, body in pending[start : start + 64]
            ]
            with audit_context(user_id=principal.subject, run_ids=(run_id,)):
                vectors.extend(checked_vectors(embedder.embed(texts), len(texts)))
        with conn.transaction():
            conn.execute("SELECT 1 FROM ingest_runs WHERE id=%s FOR UPDATE", (run_id,))
            # Short write locks serialize refreshes and keep approval, version and ACL
            # stable during insertion. Never hold these locks during provider latency.
            current = conn.execute(
                sql.SQL(
                    "SELECT e.id FROM runbook_entries e "
                    "JOIN sessions s ON s.id = e.session_id "
                    "JOIN interview_plans p ON p.id = s.plan_id "
                    "JOIN ingest_runs r ON r.id = p.run_id WHERE e.id = %s "
                    "AND e.version = %s AND e.status = 'approved' AND (%s::date IS NULL OR "
                    "e.review_by >= %s) AND "
                )
                + sql.SQL(VISIBLE_RUN)
                + sql.SQL(" FOR UPDATE OF e"),
                (entry_id, version, review_after, review_after, *visible_params(principal)),
            ).fetchone()
            if current is None:
                continue
            conn.execute(
                "DELETE FROM runbook_index WHERE entry_id = %s AND model = %s",
                (entry_id, embedder.name),
            )
            with conn.cursor() as cursor:
                cursor.executemany(
                    "INSERT INTO runbook_index "
                    "(entry_id, section, ordinal, version, model, title, body, embedding) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s::vector)",
                    [
                        (
                            entry_id,
                            name,
                            ordinal,
                            version,
                            embedder.name,
                            title,
                            body,
                            to_vector_literal(vector),
                        )
                        for (name, ordinal, body), vector in zip(pending, vectors, strict=True)
                    ],
                )
            count += len(pending)
    return count
