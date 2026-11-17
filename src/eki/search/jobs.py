"""Durable, model-specific indexing jobs with cross-process worker exclusion."""

import asyncio
import logging
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Literal
from uuid import UUID

import psycopg

from eki import pool
from eki.async_db import db
from eki.audit import AuditError, audit_context
from eki.llm import ModelError
from eki.runbook.models import RunbookEntry
from eki.search.embedding import SearchEmbedder
from eki.search.index import prepare, publish

Outcome = Literal["ready", "retrying", "obsolete"]


@dataclass
class IndexWorker:
    dsn: str = field(repr=False)
    embedder: SearchEmbedder
    domains: frozenset[str] = field(default_factory=frozenset)

    def register(self) -> None:
        """Backfill a newly selected space; approval triggers enqueue future versions."""
        with pool.connection(self.dsn) as conn, conn.transaction():
            conn.execute(
                "INSERT INTO runbook_index_models(model) VALUES (%s) ON CONFLICT DO NOTHING",
                (self.embedder.name,),
            )
            conn.execute(
                "INSERT INTO runbook_index_jobs(entry_id,version,model,status) "
                "SELECT e.id,e.version,%s,CASE WHEN EXISTS (SELECT 1 FROM runbook_index i "
                "WHERE i.entry_id=e.id AND i.version=e.version AND i.model=%s) "
                "THEN 'ready' ELSE 'pending' END FROM runbook_entries e "
                "WHERE e.status='approved' ON CONFLICT DO NOTHING",
                (self.embedder.name, self.embedder.name),
            )

    def process_one(self) -> Outcome | None:
        # A dedicated connection retains only the advisory lock during provider latency;
        # request-pool connections and row/transaction locks are released beforehand.
        with psycopg.connect(self.dsn, autocommit=True) as conn:
            with conn.transaction():
                rows = conn.execute(
                    "SELECT entry_id,version,attempts FROM runbook_index_jobs "
                    "WHERE model=%s AND (status='processing' OR "
                    "(status IN ('pending','retrying') AND available_at<=now())) "
                    "ORDER BY available_at,entry_id,version FOR UPDATE SKIP LOCKED LIMIT 100",
                    (self.embedder.name,),
                ).fetchall()
                selected = None
                for entry_id, version, attempts in rows:
                    key = f"runbook-index:{entry_id}:{version}:{self.embedder.name}"
                    locked = conn.execute(
                        "SELECT pg_try_advisory_lock(hashtextextended(%s,0))", (key,)
                    ).fetchone()
                    if locked and locked[0]:
                        selected = entry_id, version, attempts
                        conn.execute(
                            "UPDATE runbook_index_jobs SET status='processing',attempts=attempts+1,"
                            "updated_at=now(),error_code=NULL WHERE entry_id=%s AND version=%s "
                            "AND model=%s",
                            (entry_id, version, self.embedder.name),
                        )
                        break
            if selected is None:
                return None
            entry_id, version, attempts = selected
            row = conn.execute(
                "SELECT e.content,p.run_id FROM runbook_entries e JOIN sessions s ON "
                "s.id=e.session_id JOIN interview_plans p ON p.id=s.plan_id "
                "WHERE e.id=%s AND e.version=%s AND e.status='approved'",
                (entry_id, version),
            ).fetchone()
            if row is None:
                return self._status(conn, entry_id, version, "obsolete")
            content, run_id = row
            try:
                with audit_context(user_id="index-worker", run_ids=(run_id,)):
                    prepared = prepare(
                        RunbookEntry.model_validate(content), self.embedder, self.domains
                    )
            except (ModelError, AuditError) as error:
                code = (
                    "audit_unavailable" if isinstance(error, AuditError) else "provider_unavailable"
                )
                conn.execute(
                    "UPDATE runbook_index_jobs SET status='retrying',error_code=%s,"
                    "available_at=now()+%s*interval '1 second',updated_at=now() "
                    "WHERE entry_id=%s AND version=%s AND model=%s AND status='processing'",
                    (
                        code,
                        min(300, 5 * 2 ** min(attempts, 6)),
                        entry_id,
                        version,
                        self.embedder.name,
                    ),
                )
                return "retrying"
            with conn.transaction():
                # Same lock ordering as review, corrections and handover.
                conn.execute("SELECT 1 FROM ingest_runs WHERE id=%s FOR UPDATE", (run_id,))
                current = conn.execute(
                    "SELECT version,status,content FROM runbook_entries WHERE id=%s FOR UPDATE",
                    (entry_id,),
                ).fetchone()
                if current is None or current[:2] != (version, "approved"):
                    return self._status(conn, entry_id, version, "obsolete")
                if current[2] != content:
                    conn.execute(
                        "UPDATE runbook_index_jobs SET status='pending',available_at=now() "
                        "WHERE entry_id=%s AND version=%s AND model=%s",
                        (entry_id, version, self.embedder.name),
                    )
                    return "obsolete"
                publish(conn, str(entry_id), version, self.embedder.name, prepared)
                return self._status(conn, entry_id, version, "ready")

    def _status(
        self, conn: psycopg.Connection, entry: UUID, version: int, status: Outcome
    ) -> Outcome:
        conn.execute(
            "UPDATE runbook_index_jobs SET status=%s,updated_at=now(),error_code=NULL "
            "WHERE entry_id=%s AND version=%s AND model=%s",
            (status, entry, version, self.embedder.name),
        )
        return status

    def drain(self, *, limit: int = 1000) -> dict[str, int]:
        """Bounded operator/test batch. Failed jobs wait for their persisted backoff."""
        self.register()
        counts = dict.fromkeys(("ready", "retrying", "obsolete"), 0)
        for _ in range(limit):
            outcome = self.process_one()
            if outcome is None:
                break
            counts[outcome] += 1
        return counts

    async def serve(self, stop: asyncio.Event) -> None:
        registered_at = float("-inf")
        while not stop.is_set():
            try:
                if asyncio.get_running_loop().time() - registered_at >= 30:
                    await db(self.register)
                    registered_at = asyncio.get_running_loop().time()
                if await db(self.process_one) is not None:
                    continue
            except (psycopg.Error, ModelError, AuditError):
                # No provider payloads, credentials or connection strings in logs.
                logging.getLogger(__name__).debug("runbook indexing unavailable; retrying")
                registered_at = float("-inf")
            with suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=1)
