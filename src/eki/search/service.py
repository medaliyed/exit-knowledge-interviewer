"""Live ACL filtering precedes both keyword and vector candidate ranking."""

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from psycopg import sql

from eki import pool
from eki.access import VISIBLE_RUN, visible_params
from eki.audit import audit_context
from eki.auth import Principal
from eki.config import Settings, work_domains
from eki.ingest.embedding import HashingEmbedder, to_vector_literal
from eki.ingest.redaction import RedactedText, redact
from eki.runbook.maintenance import infos
from eki.runbook.models import RunbookEntry, SectionName
from eki.search.embedding import SearchEmbedder, build_embedder, checked_vectors
from eki.search.index import refresh

STOP_WORDS = frozenset(
    [
        "a",
        "an",
        "the",
        "and",
        "or",
        "is",
        "are",
        "was",
        "were",
        "be",
        "to",
        "of",
        "in",
        "on",
        "for",
        "with",
        "how",
        "do",
        "i",
        "it",
        "this",
        "that",
        "can",
        "what",
        "when",
        "my",
        "we",
        "should",
    ]
)


def query_tokens(query: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", query.lower())) - STOP_WORDS


@dataclass(frozen=True)
class SearchHit:
    entry: RunbookEntry
    version: int
    section: SectionName
    snippet: str
    score: float
    maintenance: dict[str, Any] = field(default_factory=dict)

    def public(self) -> dict[str, object]:
        return {
            **self.maintenance,
            "id": self.entry.id,
            "title": self.entry.title,
            "version": self.version,
            "section": self.section,
            "snippet": self.snippet,
            "score": self.score,
            "citations": [
                c.model_dump(mode="json")
                for claim in self.entry.section(self.section).claims
                for c in claim.citations
            ],
        }


@dataclass
class SearchService:
    dsn: str = field(repr=False)
    work_email_domains: frozenset[str] = field(default_factory=frozenset)
    embedder: SearchEmbedder = field(default_factory=HashingEmbedder)
    min_similarity: float = 0.18
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)

    def clean_query(self, query: str) -> RedactedText:
        if not query.strip() or len(query) > 2000:
            raise ValueError("query must contain 1 to 2000 characters")
        return redact(query.strip(), work_email_domains=self.work_email_domains).text

    def search(
        self, principal: Principal, query: str, *, limit: int = 5, fresh_only: bool = False
    ) -> list[SearchHit]:
        clean = self.clean_query(query)
        if not 1 <= limit <= 50:
            raise ValueError("limit must be between 1 and 50")
        if not query_tokens(clean):
            return []
        with pool.connection(self.dsn) as conn:
            refresh(
                conn,
                principal,
                self.embedder,
                self.work_email_domains,
                review_after=self.clock().astimezone(UTC).date() if fresh_only else None,
            )
            run_ids = conn.execute(
                sql.SQL(
                    "SELECT DISTINCT r.id FROM ingest_runs r "
                    "JOIN interview_plans p ON p.run_id = r.id "
                    "JOIN sessions s ON s.plan_id = p.id "
                    "JOIN runbook_entries e ON e.session_id = s.id "
                    "WHERE e.status = 'approved' AND (NOT %s OR e.review_by >= %s) AND "
                )
                + sql.SQL(VISIBLE_RUN),
                (fresh_only, self.clock().astimezone(UTC).date(), *visible_params(principal)),
            ).fetchall()
            if not run_ids:
                return []
            with audit_context(user_id=principal.subject, run_ids=tuple(r[0] for r in run_ids)):
                (vector,) = checked_vectors(self.embedder.embed([clean]), 1)
            literal = to_vector_literal(vector)
            rows = conn.execute(
                sql.SQL("""WITH eligible AS MATERIALIZED (
                    SELECT i.*, e.content FROM runbook_index i
                    JOIN runbook_entries e ON e.id = i.entry_id AND e.version = i.version
                    JOIN sessions s ON s.id = e.session_id
                    JOIN interview_plans p ON p.id = s.plan_id JOIN ingest_runs r ON r.id = p.run_id
                    WHERE e.status = 'approved' AND i.model = %s
                    AND (NOT %s OR e.review_by >= %s) AND """)
                + sql.SQL(VISIBLE_RUN)
                + sql.SQL("""
                ), q AS (SELECT websearch_to_tsquery('english', %s) AS terms,
                                %s::vector AS query_vector),
                lexical_best AS (
                    SELECT DISTINCT ON (entry_id) entry_id, section, ordinal,
                           ts_rank_cd(document, q.terms) AS relevance
                    FROM eligible, q WHERE document @@ q.terms
                    ORDER BY entry_id, relevance DESC, section, ordinal
                ), semantic_best AS (
                    SELECT DISTINCT ON (entry_id) entry_id, section, ordinal,
                           embedding <=> q.query_vector AS distance
                    FROM eligible, q WHERE (embedding <=> q.query_vector) <= 1 - %s
                    ORDER BY entry_id, distance, section, ordinal
                ), lexical AS (
                    SELECT entry_id, section, ordinal, 0 AS kind,
                           row_number() OVER (ORDER BY relevance DESC, entry_id) AS rank
                    FROM lexical_best
                ), semantic AS (
                    SELECT entry_id, section, ordinal, 1 AS kind,
                           row_number() OVER (ORDER BY distance, entry_id) AS rank
                    FROM semantic_best
                ), channels AS (SELECT * FROM lexical UNION ALL SELECT * FROM semantic),
                fused AS (
                    SELECT entry_id, sum(1.0 / (60 + rank)) AS score
                    FROM channels GROUP BY entry_id
                ), representative AS (
                    SELECT DISTINCT ON (c.entry_id) c.entry_id, c.section, c.ordinal, f.score
                    FROM channels c JOIN fused f USING (entry_id)
                    ORDER BY c.entry_id, c.rank, c.kind, c.section, c.ordinal
                ) SELECT e.content, e.version, e.section, e.body, r.score
                  FROM representative r JOIN eligible e USING (entry_id, section, ordinal)
                  ORDER BY r.score DESC, r.entry_id LIMIT %s"""),
                (
                    self.embedder.name,
                    fresh_only,
                    self.clock().astimezone(UTC).date(),
                    *visible_params(principal),
                    clean,
                    literal,
                    self.min_similarity,
                    limit,
                ),
            ).fetchall()
            entries = [RunbookEntry.model_validate(r[0]) for r in rows]
            metadata = infos(conn, [e.id for e in entries], self.clock())
            return [
                SearchHit(entry, r[1], r[2], r[3], float(r[4]), metadata.get(entry.id, {}))
                for entry, r in zip(entries, rows, strict=True)
            ]

    def has_overdue(self, principal: Principal, query: str) -> bool:
        with pool.connection(self.dsn) as conn:
            return (
                conn.execute(
                    sql.SQL(
                        "SELECT 1 FROM runbook_entries e JOIN sessions s ON s.id=e.session_id "
                        "JOIN interview_plans p ON p.id=s.plan_id JOIN ingest_runs r ON "
                        "r.id=p.run_id WHERE e.status='approved' AND e.review_by<%s AND "
                        "to_tsvector('english',e.title||' '||e.content::text) @@ "
                        "websearch_to_tsquery('english',%s) AND "
                    )
                    + sql.SQL(VISIBLE_RUN)
                    + sql.SQL(" LIMIT 1"),
                    (
                        self.clock().astimezone(UTC).date(),
                        self.clean_query(query),
                        *visible_params(principal),
                    ),
                ).fetchone()
                is not None
            )


def build_search(settings: Settings) -> SearchService:
    return SearchService(
        settings.database_url.get_secret_value(),
        work_domains(settings),
        build_embedder(settings),
        settings.search_min_similarity,
    )
