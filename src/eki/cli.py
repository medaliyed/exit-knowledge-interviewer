"""The ``eki`` command line: run the phase 1 pipeline.

    eki migrate                      apply database migrations
    eki ingest --since D --until D   read the expert's git history and tickets, redact, store, index
    eki map --run ID                 build topics and gap scores for a run
    eki gaps --run ID [--json]       show the ranked topics

The expert, the work email domains and the source directory come from settings
(``EKI_EXPERT_NAME`` and friends) and can be overridden on the command line.
Output shows labels, counts and scores. It never shows artifact bodies.
"""

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import psycopg

from eki.config import get_settings, work_domains
from eki.db import connect, migrate
from eki.ingest.embedding import HashingEmbedder
from eki.ingest.index import index_run
from eki.ingest.pipeline import IngestError, ingest_from_source, load, store
from eki.ingest.scope import Scope
from eki.mapping.scoring import score_topics
from eki.mapping.store import load_mapping, save_mapping
from eki.mapping.topics import build_topics

USAGE_ERROR = 2
FAILURE = 1


def _day(text: str) -> datetime:
    try:
        return datetime.fromisoformat(text).replace(tzinfo=UTC)
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"not a date (YYYY-MM-DD): {text}") from error


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="eki", description="Exit-Knowledge Interviewer")
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("migrate", help="apply database migrations")

    ingest = commands.add_parser("ingest", help="read, redact, store and index one expert's work")
    ingest.add_argument("--since", type=_day, required=True, help="first day, YYYY-MM-DD")
    ingest.add_argument("--until", type=_day, required=True, help="last day, inclusive")
    ingest.add_argument("--expert-name")
    ingest.add_argument("--expert-email")
    ingest.add_argument("--source-dir", type=Path)

    mapping = commands.add_parser("map", help="build topics and gap scores for a run")
    mapping.add_argument("--run", type=UUID, required=True)

    gaps = commands.add_parser("gaps", help="show the ranked topics of a run")
    gaps.add_argument("--run", type=UUID, required=True)
    gaps.add_argument("--json", action="store_true", help="machine-readable output")
    gaps.add_argument("--limit", type=int, default=None, help="show only the top N")
    return parser


def _fail(message: str, code: int = FAILURE) -> int:
    print(f"eki: {message}", file=sys.stderr)
    return code


def _migrate(conn: psycopg.Connection) -> int:
    applied = migrate(conn)
    print("Applied: " + ", ".join(applied) if applied else "Database is up to date.")
    return 0


def _ingest(conn: psycopg.Connection, arguments: argparse.Namespace) -> int:
    settings = get_settings()
    name = arguments.expert_name or settings.expert_name
    email = arguments.expert_email or settings.expert_email
    source_dir = arguments.source_dir or settings.source_dir
    if not name or not email:
        return _fail("no expert given: set EKI_EXPERT_NAME and EKI_EXPERT_EMAIL", USAGE_ERROR)
    if source_dir is None or not Path(source_dir).is_dir():
        return _fail("no source directory: set EKI_SOURCE_DIR to an existing folder", USAGE_ERROR)
    until = arguments.until.replace(hour=23, minute=59, second=59)
    if arguments.since > until:
        return _fail("--since must be on or before --until", USAGE_ERROR)
    scope = Scope(
        expert_name=name,
        expert_email=email,
        since=arguments.since,
        until=until,
        work_email_domains=work_domains(settings),
    )
    snapshot = asyncio.run(ingest_from_source(scope, Path(source_dir).resolve()))
    run_id = store(conn, snapshot)
    chunks = index_run(conn, run_id, HashingEmbedder())
    conn.commit()
    print(f"Ingested {len(snapshot.artifacts)} artifacts and indexed {chunks} chunks.")
    print(run_id)
    return 0


def _map(conn: psycopg.Connection, run_id: UUID) -> int:
    snapshot = load(conn, run_id)
    topics = build_topics(snapshot, HashingEmbedder())
    scores = score_topics(topics, snapshot, as_of=snapshot.until)
    save_mapping(conn, run_id, topics, scores)
    conn.commit()
    print(f"Mapped {len(topics)} topics and scored them.")
    return 0


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


def _gaps(conn: psycopg.Connection, arguments: argparse.Namespace) -> int:
    rows = _gap_rows(conn, arguments.run)
    if rows is None:
        return _fail(f"run {arguments.run} has no topics yet; run: eki map --run {arguments.run}")
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
        label = f"{row['system'] or '-'}: {row['label']}"
        print(
            f"{row['rank']:>4} {row['score']:>6.1f} {row['exclusivity']:>5.2f} "
            f"{row['doc_gap']:>5.2f} {row['recency']:>5.2f} {row['criticality']:>5.2f}  "
            f"{row['doc_status']:<10} {row['kind']:<9} {row['artifact_count']:>3}  {label[:70]}"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        with connect(get_settings()) as conn:
            if arguments.command == "migrate":
                return _migrate(conn)
            if arguments.command == "ingest":
                return _ingest(conn, arguments)
            if arguments.command == "map":
                return _map(conn, arguments.run)
            return _gaps(conn, arguments)
    except IngestError as error:
        return _fail(str(error))
    except psycopg.OperationalError:
        return _fail("cannot reach the database; is `docker compose up -d postgres` running?")


if __name__ == "__main__":
    sys.exit(main())
