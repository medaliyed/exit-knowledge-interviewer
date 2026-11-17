"""``eki migrate``, ``ingest``, ``map``, ``gaps`` and ``reindex-artifacts``."""

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import psycopg

from eki.audit import (
    AuditDetails,
    AuditRecorder,
    append_event,
    audit_context,
)
from eki.cli.common import USAGE_ERROR, fail
from eki.config import Settings, work_domains
from eki.db import migrate
from eki.graph.workflow import setup_checkpoints
from eki.ingest.embedding import HashingEmbedder
from eki.ingest.index import index_run
from eki.ingest.pipeline import ingest_from_source, load, store
from eki.ingest.scope import Scope, validate_mail_folders
from eki.ingest.semantic import configured as artifact_embedder
from eki.interview import store as interviews
from eki.interview.service import (
    NotFoundError,
)
from eki.mapping.scoring import score_topics
from eki.mapping.store import load_mapping, save_mapping
from eki.mapping.topics import build_topics


def cmd_migrate(conn: psycopg.Connection, settings: Settings) -> int:
    applied = migrate(conn)
    setup_checkpoints(settings.database_url.get_secret_value())
    print("Applied: " + ", ".join(applied) if applied else "Database is up to date.")
    return 0


def cmd_ingest(conn: psycopg.Connection, arguments: argparse.Namespace, settings: Settings) -> int:
    name = arguments.expert_name or settings.expert_name
    email = arguments.expert_email or settings.expert_email
    source_dir = arguments.source_dir or settings.source_dir
    if not name or not email:
        return fail("no expert given: set EKI_EXPERT_NAME and EKI_EXPERT_EMAIL", USAGE_ERROR)
    if source_dir is None or not Path(source_dir).is_dir():
        return fail("no source directory: set EKI_SOURCE_DIR to an existing folder", USAGE_ERROR)
    until = arguments.until.replace(hour=23, minute=59, second=59)
    if arguments.since > until:
        return fail("--since must be on or before --until", USAGE_ERROR)
    scope = Scope(
        expert_name=name,
        expert_email=email,
        since=arguments.since,
        until=until,
        work_email_domains=work_domains(settings),
        mail_folders=validate_mail_folders(tuple(arguments.mail_folder)),
    )
    run_id, call_id = uuid4(), uuid4()
    recorder = AuditRecorder(settings.database_url.get_secret_value())
    with audit_context(run_ids=(run_id,), recorder=recorder):
        recorder.record("ingest", "started", AuditDetails(operation="ingest"), call_id=call_id)
        try:
            snapshot = asyncio.run(ingest_from_source(scope, Path(source_dir).resolve()))
            with conn.transaction():
                store(conn, snapshot, run_id=run_id)
                chunks = index_run(conn, run_id, artifact_embedder(settings, run_id=run_id))
                append_event(
                    conn,
                    "ingest",
                    "completed",
                    AuditDetails(operation="ingest", count=len(snapshot.artifacts)),
                    call_id=call_id,
                )
        except BaseException:
            conn.rollback()
            recorder.record("ingest", "failed", AuditDetails(operation="ingest"), call_id=call_id)
            raise
    print(f"Ingested {len(snapshot.artifacts)} artifacts and indexed {chunks} chunks.")
    print(run_id)
    return 0


def cmd_map(conn: psycopg.Connection, run_id: UUID) -> int:
    snapshot = load(conn, run_id)
    topics = build_topics(snapshot, HashingEmbedder())
    scores = score_topics(topics, snapshot, as_of=snapshot.until)
    save_mapping(conn, run_id, topics, scores)
    conn.commit()
    print(f"Mapped {len(topics)} topics and scored them.")
    return 0


def topic_name(system: str | None, label: str) -> str:
    """``system: label``, or the system alone for a system topic, whose label is its name."""
    return label if label == system else f"{system or '-'}: {label}"


def _gap_rows(conn: psycopg.Connection, run_id: UUID) -> list[dict[str, Any]] | None:
    load(conn, run_id)  # raises IngestError for an unknown run
    topics, scores = load_mapping(conn, run_id)
    if not topics:
        return None
    by_id = {topic.id: topic for topic in topics}
    return [
        {
            "rank": rank,
            "topic_id": score.topic_id,
            "kind": by_id[score.topic_id].kind.value,
            "system": by_id[score.topic_id].system,
            "label": by_id[score.topic_id].label,
            "artifact_count": len(by_id[score.topic_id].artifact_ids),
            "score": score.score,
            "exclusivity": score.exclusivity,
            "doc_gap": score.doc_gap,
            "recency": score.recency,
            "criticality": score.criticality,
            "doc_status": score.doc_status.value,
            "linked_documents": list(score.linked_documents),
            "explanation": score.explanation,
        }
        for rank, score in enumerate(scores, start=1)
    ]


def cmd_gaps(conn: psycopg.Connection, arguments: argparse.Namespace) -> int:
    rows = _gap_rows(conn, arguments.run)
    if rows is None:
        return fail(f"run {arguments.run} has no topics yet; run: eki map --run {arguments.run}")
    if arguments.limit is not None:
        rows = rows[: max(arguments.limit, 0)]
    if arguments.json:
        print(json.dumps(rows, indent=2))
        return 0
    header = (
        f"{'rank':>4} {'score':>6} {'excl':>5} {'doc':>5} {'rec':>5} {'crit':>5}  "
        f"{'doc status':<10} {'kind':<9} {'n':>3}  topic"
    )
    print(header)
    print("-" * len(header))
    for row in rows:
        label = topic_name(row["system"], row["label"])
        print(
            f"{row['rank']:>4} {row['score']:>6.1f} {row['exclusivity']:>5.2f} "
            f"{row['doc_gap']:>5.2f} {row['recency']:>5.2f} {row['criticality']:>5.2f}  "
            f"{row['doc_status']:<10} {row['kind']:<9} {row['artifact_count']:>3}  {label[:70]}"
        )
    return 0


def cmd_reindex(conn: psycopg.Connection, arguments: argparse.Namespace, settings: Settings) -> int:
    """Replace a run's artifact vectors with the configured artifact embedder."""
    if not interviews.run_exists(conn, arguments.run):
        raise NotFoundError("no completed run")
    with audit_context(run_ids=(arguments.run,)):
        embedder = artifact_embedder(settings, run_id=arguments.run)
        count = index_run(conn, arguments.run, embedder)
    print(f"Indexed {count} artifact chunks with {embedder.name}.")
    return 0
