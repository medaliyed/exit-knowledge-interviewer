"""The ``eki`` command line: ingest, map, plan and interview.

    eki migrate                      apply database migrations
    eki ingest --since D --until D   read the expert's git history and tickets, redact, store, index
    eki map --run ID                 build topics and gap scores for a run
    eki gaps --run ID [--json]       show the ranked topics
    eki plan --run ID                draft an interview plan (calls the drafting model)
    eki interview --plan ID          approve the plan and run the interview in the terminal

The expert, the work email domains and the source directory come from settings
(``EKI_EXPERT_NAME`` and friends) and can be overridden on the command line.
Output shows labels, counts, scores, drafted questions with the short passage
each one quotes, and the interview itself. It never shows whole artifact bodies.
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

from eki.config import ConfigError, get_settings, work_domains
from eki.db import connect, migrate
from eki.graph.workflow import setup_checkpoints
from eki.ingest.embedding import HashingEmbedder
from eki.ingest.index import index_run
from eki.ingest.pipeline import IngestError, ingest_from_source, load, store
from eki.ingest.scope import Scope
from eki.interview.models import InterviewPlan
from eki.interview.service import (
    ConflictError,
    Event,
    InterviewService,
    NotFoundError,
    SessionView,
    build_service,
)
from eki.llm import ModelError
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

    plan = commands.add_parser("plan", help="draft an interview plan for a mapped run")
    plan.add_argument("--run", type=UUID, required=True)

    interview = commands.add_parser("interview", help="approve a plan and run its interview")
    interview.add_argument("--plan", type=UUID, required=True)
    interview.add_argument("--approve-as", help="who approves the plan (default: the expert)")
    return parser


def _fail(message: str, code: int = FAILURE) -> int:
    print(f"eki: {message}", file=sys.stderr)
    return code


def _migrate(conn: psycopg.Connection) -> int:
    applied = migrate(conn)
    setup_checkpoints(get_settings().database_url.get_secret_value())
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


def _show_plan(plan: InterviewPlan) -> None:
    print(f"Plan {plan.id} ({plan.status}), {len(plan.topics)} topics")
    for position, topic in enumerate(plan.topics, start=1):
        print(f"{position:>3}. {topic.system or '-'}: {topic.label}  ({topic.reason})")
        for question in topic.questions:
            print(f"       Q: {question.text}")
            if question.artifact_id and question.quote:
                print(f'          from {question.artifact_id}: "{question.quote}"')
        for reason in topic.dropped:
            print(f"       dropped a drafted question: {reason}")


async def _plan(service: InterviewService, run_id: UUID) -> int:
    plan = await service.draft_plan(run_id)
    _show_plan(plan)
    print(f"Review it, then run: eki interview --plan {plan.id}")
    return 0


def _show_event(event: Event) -> None:
    kind = event["type"]
    if kind == "topic":
        print(f"\n--- Topic {event['index']} of {event['total']}: {event['label']} ---")
    elif kind == "note":
        detail = "confidential" if event.get("confidential") else event.get("text", "")
        print(f"  noted {event['section']}: {detail}")
    elif kind == "open":
        print(f"  left open {event['section']}: {event['reason']}")
    elif kind == "corrected":
        sections = ", ".join(event["sections"]) or "no note"
        print(f"  turn {event['corrects']} corrected ({sections} updated)")
    elif kind in ("error", "recovered"):
        print(f"  {event['message']}")


HELP = (
    "Type your answer. Other commands: /skip, /confidential ANSWER, "
    "/correct TURN NEW-TEXT, /quit (you can continue later)."
)


async def _say(service: InterviewService, session_id: UUID, line: str) -> None:
    """Send one line typed by the expert and print what the interviewer did with it."""
    command, _, rest = line.partition(" ")
    try:
        if command == "/skip":
            stream = await service.post(session_id, "skip")
        elif command == "/confidential":
            stream = await service.post(session_id, "answer", rest, confidential=True)
        elif command == "/correct":
            number, _, text = rest.strip().partition(" ")
            if not number.isdigit():
                print("  usage: /correct TURN NEW-TEXT")
                return
            stream = await service.post(session_id, "correct", text, corrects=int(number))
        else:
            stream = await service.post(session_id, "answer", line)
    except (ValueError, NotFoundError, ConflictError) as refused:
        print(f"  not accepted: {refused}")
        return
    async for event in stream:
        _show_event(event)


async def _interview(service: InterviewService, plan_id: UUID, approver: str | None) -> int:
    plan = await service.get_plan(plan_id)
    session_id = await service.session_for_plan(plan_id)
    if session_id is None:
        if not approver:
            return _fail("nobody to approve the plan: set EKI_EXPERT_NAME or --approve-as", 2)
        _show_plan(plan)
        view: SessionView = await service.approve(plan_id, approver)
        print(f"Plan approved by {approver}.")
        session_id = view.session.id
    print(HELP)
    while True:
        view = await service.view(session_id)
        if view.session.status == "finished":
            print(
                f"\nInterview finished: {len(view.notes)} sections noted, "
                f"{len(view.open_items)} left open."
            )
            return 0
        if view.waiting is None:
            # The process stopped mid-step last time: let the workflow reach its question.
            await _say(service, session_id, "(continue)")
            continue
        print(f"\n[turn {view.waiting.ordinal}, {view.waiting.section}] {view.waiting.text}")
        try:
            line = input("> ").strip()
        except EOFError:
            line = "/quit"
        if line == "/quit":
            print(f"Stopped. Continue with: eki interview --plan {plan_id}")
            return 0
        if line:
            await _say(service, session_id, line)


def _interview_commands(arguments: argparse.Namespace) -> int:
    settings = get_settings()
    service = build_service(settings)
    try:
        if arguments.command == "plan":
            return asyncio.run(_plan(service, arguments.run))
        approver = arguments.approve_as or settings.expert_name
        return asyncio.run(_interview(service, arguments.plan, approver))
    finally:
        service.tracer.flush()


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command in ("plan", "interview"):
            return _interview_commands(arguments)
        with connect(get_settings()) as conn:
            if arguments.command == "migrate":
                return _migrate(conn)
            if arguments.command == "ingest":
                return _ingest(conn, arguments)
            if arguments.command == "map":
                return _map(conn, arguments.run)
            return _gaps(conn, arguments)
    except ConfigError as error:
        return _fail(str(error), USAGE_ERROR)
    except (IngestError, NotFoundError, ConflictError) as error:
        return _fail(str(error))
    except ModelError:
        return _fail("the model could not be reached; nothing was lost, run the command again")
    except psycopg.OperationalError:
        return _fail("cannot reach the database; is `docker compose up -d postgres` running?")


if __name__ == "__main__":
    sys.exit(main())
