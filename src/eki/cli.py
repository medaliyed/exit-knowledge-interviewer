"""The ``eki`` command line: ingest, map, plan and interview.

    eki migrate                      apply database migrations
    eki ingest --since D --until D   read the expert's git history and tickets, redact, store, index
    eki map --run ID                 build topics and gap scores for a run
    eki gaps --run ID [--json]       show the ranked topics
    eki plan --run ID                draft an interview plan (calls the drafting model)
    eki interview --plan ID          approve the plan and run the interview in the terminal
    eki entries --plan ID            list runbook entries and their review status
    eki review --plan ID             review draft entries; quit and resume

The expert, the work email domains and the source directory come from settings
(``EKI_EXPERT_NAME`` and friends) and can be overridden on the command line.
Output shows labels, counts, scores, drafted questions with the short passage
each one quotes, and the interview itself. It never shows whole artifact bodies.
"""

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import psycopg

from eki import access, accounts, followup_cli, maintenance
from eki.audit import (
    AuditDetails,
    AuditError,
    AuditRecorder,
    append_event,
    audit_context,
    list_auth_events,
    list_events,
)
from eki.auth import (
    AuthenticationError,
    Principal,
    TokenAuthenticator,
    acting_as,
    current_principal,
)
from eki.config import ConfigError, get_settings, work_domains
from eki.db import connect, migrate
from eki.feedback import FeedbackService
from eki.graph.workflow import setup_checkpoints
from eki.ingest.embedding import HashingEmbedder
from eki.ingest.index import index_run
from eki.ingest.pipeline import IngestError, ingest_from_source, load, store
from eki.ingest.scope import Scope, validate_mail_folders
from eki.ingest.semantic import configured as artifact_embedder
from eki.interview import generation
from eki.interview import store as interviews
from eki.interview.models import InterviewPlan
from eki.interview.plan import PlanError
from eki.interview.service import (
    ConflictError,
    Event,
    GenerationFailed,
    InterviewService,
    NotFoundError,
    SessionView,
    build_service,
)
from eki.llm import ModelError
from eki.llm.tracing import flush_tracer
from eki.mapping.scoring import score_topics
from eki.mapping.store import load_mapping, save_mapping
from eki.mapping.topics import build_topics
from eki.runbook.export import ExportService
from eki.runbook.faithfulness import Unsupported
from eki.runbook.models import SECTION_NAMES, EntryStatus, InterviewTurnCitation
from eki.runbook.review import SectionEdit
from eki.runbook.service import RunbookService
from eki.runbook.store import StoredEntry
from eki.search.answers import build_answers
from eki.search.service import build_search

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
    ingest.add_argument(
        "--mail-folder",
        action="append",
        default=[],
        help="explicit normal-mail folder; repeat to select more",
    )

    reindex = commands.add_parser(
        "reindex-artifacts", help="rebuild an artifact index with the configured model"
    )
    reindex.add_argument("--run", type=UUID, required=True)

    mapping = commands.add_parser("map", help="build topics and gap scores for a run")
    mapping.add_argument("--run", type=UUID, required=True)

    gaps = commands.add_parser("gaps", help="show the ranked topics of a run")
    gaps.add_argument("--run", type=UUID, required=True)
    gaps.add_argument("--json", action="store_true", help="machine-readable output")
    gaps.add_argument("--limit", type=int, default=None, help="show only the top N")

    plan = commands.add_parser("plan", help="draft an interview plan for a mapped run")
    plan.add_argument("--run", type=UUID, required=True)
    mode = plan.add_mutually_exclusive_group()
    mode.add_argument("--resume-generation", type=UUID, help="resume a saved generation")
    mode.add_argument("--generation-status", type=UUID, help="inspect progress without model calls")

    interview = commands.add_parser("interview", help="approve a plan and run its interview")
    interview.add_argument("--plan", type=UUID, required=True)
    interview.add_argument("--approve-as", help="who approves the plan (default: the expert)")
    entries = commands.add_parser("entries", help="list runbook entries and review status")
    entries.add_argument("--plan", type=UUID, required=True)
    review = commands.add_parser("review", help="review draft runbook entries; quit and resume")
    review_target = review.add_mutually_exclusive_group(required=True)
    review_target.add_argument("--plan", type=UUID)
    review_target.add_argument("--run", type=UUID)
    review.add_argument("--due", action="store_true")
    review.add_argument("--review-by", type=date.fromisoformat)
    upkeep = commands.add_parser("maintenance", help="inspect the maintenance queue")
    upkeep.add_argument("--run", type=UUID, required=True)
    upkeep.add_argument("--offset", type=int, default=0)
    upkeep.add_argument(
        "--filter", choices=("all", "overdue", "due_soon", "due", "flagged"), default="all"
    )
    review.add_argument("--review-as", help="reviewer's name (default: the run's expert)")
    handover = commands.add_parser("handover", help="transfer a run into maintenance")
    handover.add_argument("--run", type=UUID, required=True)
    handover.add_argument("--to", required=True)
    ownership = handover.add_mutually_exclusive_group(required=True)
    ownership.add_argument("--expected-owner")
    ownership.add_argument("--unassigned", action="store_true")
    handover.add_argument("--expected-run-revision", type=int, required=True)
    handover.add_argument("--reason", required=True)
    permissions = commands.add_parser("access", help="set run ownership/readers (local operator)")
    permissions.add_argument("--run", type=UUID, required=True)
    permissions.add_argument("--owner")
    permissions.add_argument("--expected-owner")
    permissions.add_argument("--expected-run-revision", type=int)
    permissions.add_argument("--reason", default="Owner assignment")
    permissions.add_argument(
        "--readers", nargs="*", default=None, help="replace reader subjects; empty revokes all"
    )
    users = commands.add_parser("users", help="manage browser accounts (local operator)")
    user_commands = users.add_subparsers(dest="user_command", required=True)
    add_user = user_commands.add_parser(
        "add", help="create an account and print its invitation link"
    )
    add_user.add_argument("--email", required=True)
    add_user.add_argument("--name", required=True)
    add_user.add_argument("--subject", required=True, help="stable ACL identity; cannot be changed")
    add_user.add_argument("--admin", action="store_true")
    add_user.add_argument("--allow-external-domain", action="store_true")
    update_user = user_commands.add_parser(
        "update", help="change an account's email or display name"
    )
    update_user.add_argument(
        "--email", required=True, help="current email used to find the account"
    )
    update_user.add_argument("--new-email")
    update_user.add_argument("--name")
    update_user.add_argument("--allow-external-domain", action="store_true")
    for command, help_text in (
        ("reset-password", "print a single-use reset link"),
        ("disable", "disable an account and revoke all sessions"),
        ("enable", "enable an account"),
        ("revoke-sessions", "sign the account out on every device"),
    ):
        user_commands.add_parser(command, help=help_text).add_argument("--email", required=True)
    user_commands.add_parser("list", help="list accounts and their sign-in status")
    search = commands.add_parser("search", help="search accessible approved runbook entries")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=5)
    search.add_argument("--json", action="store_true")
    ask = commands.add_parser("ask", help="answer from cited approved runbook claims")
    ask.add_argument("question")
    ask.add_argument("--json", action="store_true")
    export = commands.add_parser("export", help="export accessible approved entries as Markdown")
    selection = export.add_mutually_exclusive_group(required=True)
    selection.add_argument("--entry", type=UUID)
    selection.add_argument("--run", type=UUID)
    export.add_argument(
        "--output", type=Path, help="new file; existing files are never overwritten"
    )
    audit = commands.add_parser(
        "audit", help="read a run's or authentication metadata-only audit log (local operator)"
    )
    audit_scope = audit.add_mutually_exclusive_group(required=True)
    audit_scope.add_argument("--run", type=UUID)
    audit_scope.add_argument(
        "--auth", action="store_true", help="list run-less authentication events"
    )
    audit.add_argument("--after", type=int, default=0)
    audit.add_argument("--limit", type=int, default=100)
    followup_cli.add_parser(commands)
    return parser


def _fail(message: str, code: int = FAILURE) -> int:
    print(f"eki: {message}", file=sys.stderr)
    return code


def _users(conn: psycopg.Connection, arguments: argparse.Namespace) -> int:
    settings = get_settings()
    command = arguments.user_command
    if command == "list":
        print("Email\tSubject\tName\tAdmin\tDisabled\tLast sign-in")
        for account in accounts.list_accounts(conn):
            last_sign_in = (
                account.last_sign_in_at.isoformat() if account.last_sign_in_at else "never"
            )
            print(
                f"{account.email}\t{account.subject}\t{account.name}\t"
                f"{str(account.admin).lower()}\t{str(account.disabled).lower()}\t{last_sign_in}"
            )
        return 0
    if command == "update":
        if arguments.new_email is None and arguments.name is None:
            raise ValueError("supply --new-email or --name to update an account")
        account = accounts.update_account(
            conn,
            arguments.email,
            new_email=arguments.new_email,
            name=arguments.name,
            work_domains=work_domains(settings),
            allow_external_domain=arguments.allow_external_domain,
            bearer_identities=TokenAuthenticator(settings.api_tokens.get_secret_value()).identities,
        )
        print(f"Account {account.subject} updated.")
        return 0
    if command in ("add", "reset-password"):
        if not settings.public_origin:
            raise ConfigError("set EKI_PUBLIC_ORIGIN before creating an invitation or reset link")
        now = datetime.now(UTC)
        # Account creation and its invitation either both commit or neither does.
        with conn.transaction():
            if command == "add":
                account = accounts.create_account(
                    conn,
                    email=arguments.email,
                    name=arguments.name,
                    subject=arguments.subject,
                    admin=arguments.admin,
                    allow_external_domain=arguments.allow_external_domain,
                    work_domains=work_domains(settings),
                    bearer_identities=list(
                        TokenAuthenticator(settings.api_tokens.get_secret_value()).identities
                    ),
                    now=now,
                )
            else:
                account = accounts.get_account(conn, arguments.email)
            purpose: accounts.Purpose = "invite" if command == "add" else "reset"
            token = accounts.create_link(conn, account.id, purpose, now=now)
        # Do not print a usable link until the transaction has committed.
        conn.commit()
        print(
            "Invitation link (valid for 72 hours):"
            if command == "add"
            else "Reset link (valid for 1 hour):"
        )
        print(f"{settings.public_origin.rstrip('/')}/workspace/set-password#token={token}")
        return 0
    if command in ("disable", "enable"):
        account = accounts.set_disabled(conn, arguments.email, command == "disable")
        print(f"Account {account.subject} {'disabled' if account.disabled else 'enabled'}.")
        return 0
    account = accounts.get_account(conn, arguments.email)
    count = accounts.revoke_sessions(conn, account.id)
    print(f"Revoked {count} session(s) for {account.subject}.")
    return 0


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
    if plan.kind == "follow_up" and plan.limits is not None:
        print(
            f"Follow-up from session {plan.parent_session_id}; revision {plan.revision}. "
            f"Limits: {plan.limits.questions} questions, {plan.limits.model_calls} model calls, "
            f"{plan.limits.embedding_calls} embedding calls."
        )
    for position, topic in enumerate(plan.topics, start=1):
        print(f"{position:>3}. {topic.system or '-'}: {topic.label}  ({topic.reason})")
        if topic.sections is not None:
            print("       Selected sections: " + ", ".join(topic.sections))
        for question in topic.questions:
            print(f"       Q: {question.text}")
            if question.artifact_id and question.quote:
                print(f'          from {question.artifact_id}: "{question.quote}"')
        for reason in topic.dropped:
            print(f"       dropped a drafted question: {reason}")


async def _plan(service: InterviewService, run_id: UUID, resume: UUID | None = None) -> int:
    gid = resume or uuid4()
    if resume is not None:
        await service.generation_status(run_id, gid)
    print(f"Generation {gid}. Resume with: eki plan --run {run_id} --resume-generation {gid}")
    try:
        plan = await service.draft_plan(run_id, generation_id=gid)
    except GenerationFailed:
        return _fail("Drafting paused; completed topics are saved. Use the resume command above.")
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
    elif kind == "delivery":
        print(f"  message {event['id']}: {event['status']}")
    elif kind in ("error", "recovered"):
        print(f"  {event['message']}")


HELP = (
    "Type your answer. Other commands: /skip, /confidential ANSWER, "
    "/correct TURN NEW-TEXT, /quit (you can continue later)."
)


async def _say(service: InterviewService, session_id: UUID, line: str) -> None:
    """Send one line typed by the expert and print what the interviewer did with it."""
    command, _, rest = line.partition(" ")
    view = await service.view(session_id)
    options: dict[str, Any] = {"message_id": uuid4()}
    if view.waiting is not None:
        options["question_turn"] = view.waiting.ordinal
    try:
        if command == "/skip":
            stream = await service.post(session_id, "skip", **options)
        elif command == "/confidential":
            stream = await service.post(session_id, "answer", rest, confidential=True, **options)
        elif command == "/correct":
            number, _, text = rest.strip().partition(" ")
            if not number.isdigit():
                print("  usage: /correct TURN NEW-TEXT")
                return
            stream = await service.post(
                session_id, "correct", text, corrects=int(number), message_id=options["message_id"]
            )
        else:
            stream = await service.post(session_id, "answer", line, **options)
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
        view: SessionView = await service.approve(
            plan_id, approver, expected_revision=plan.revision if plan.kind == "follow_up" else None
        )
        print(f"Plan approved by {approver}.")
        session_id = view.session.id
    print(HELP)
    if plan.kind == "follow_up":
        print("Follow-up: /stop ends further questions and retains captured answers.")
    while True:
        pending = await service.receipts(session_id)
        if pending:
            print(f"Saved message {pending[0]['id']}: {pending[0]['status']}.")
            try:
                choice = input("/retry to resume it, /quit to continue later: ").strip()
            except EOFError:
                choice = "/quit"
            if choice != "/retry":
                print(f"Stopped. Continue with: eki interview --plan {plan_id}")
                return 0
            stream = await service.resume(session_id, UUID(pending[0]["id"]))
            async for event in stream:
                _show_event(event)
            continue
        view = await service.view(session_id)
        if view.session.status == "finished":
            print(
                f"\nInterview finished: {len(view.notes)} sections noted, "
                f"{len(view.open_items)} left open."
            )
            if view.usage is not None:
                print(json.dumps(view.usage, default=str))
                print(
                    f"Review unpublished proposals: eki follow-up proposals --session {session_id}"
                )
                print(f"New-topic drafts: eki review --plan {plan_id}")
            return 0
        if view.waiting is None:
            # The process stopped mid-step last time: let the workflow reach its question.
            stream = await service.resume(session_id)
            async for event in stream:
                _show_event(event)
            continue
        if view.usage is not None:
            print(
                f"Follow-up scope: {', '.join(view.scope)}; "
                f"questions {view.usage['questions']}/{view.usage['limits']['questions']}, "
                f"model calls {view.usage['model_calls']}/{view.usage['limits']['model_calls']}."
            )
        print(f"\n[turn {view.waiting.ordinal}, {view.waiting.section}] {view.waiting.text}")
        try:
            line = input("> ").strip()
        except EOFError:
            line = "/quit"
        if line == "/quit":
            print(f"Stopped. Continue with: eki interview --plan {plan_id}")
            return 0
        if line == "/stop" and plan.kind == "follow_up":
            principal = current_principal()
            if principal is None:
                raise AuthenticationError("follow-up identity required")
            from eki.followup.sessions import FollowUpService

            FollowUpService(service.dsn, service.work_email_domains).stop(session_id, principal)
            continue
        if line:
            await _say(service, session_id, line)


def _interview_commands(arguments: argparse.Namespace) -> int:
    settings = get_settings()
    if arguments.command == "plan" and arguments.generation_status is not None:
        with connect(settings) as conn:
            print(json.dumps(generation.read(conn, arguments.run, arguments.generation_status)))
        return 0
    if arguments.command == "interview":
        with connect(settings) as conn:
            plan = interviews.load_plan(conn, arguments.plan)
        if plan is None:
            raise NotFoundError(f"no plan {arguments.plan}")
        if plan.kind == "follow_up":
            principal = followup_cli.principal(settings)
            with acting_as(principal), audit_context(user_id=principal.subject):
                from eki.followup.sessions import respondent

                with connect(settings) as conn:
                    respondent(conn, plan.run_id, principal)
                if arguments.approve_as and arguments.approve_as != principal.name:
                    raise ValueError("--approve-as cannot impersonate another expert")
                service = build_service(settings)
                try:
                    return asyncio.run(_interview(service, plan.id, principal.name))
                finally:
                    flush_tracer(service.tracer)
    service = build_service(settings)
    try:
        if arguments.command == "plan":
            return asyncio.run(_plan(service, arguments.run, arguments.resume_generation))
        approver = arguments.approve_as or settings.expert_name
        return asyncio.run(_interview(service, arguments.plan, approver))
    finally:
        flush_tracer(service.tracer)


def _show_entry(stored: StoredEntry, unsupported: Sequence[Unsupported] = ()) -> None:
    entry = stored.entry
    doubts = {(u.section, u.index): u.terms for u in unsupported}
    print(f"\n{entry.title} ({entry.status.value}, version {stored.version}) [{entry.id}]")
    for name in SECTION_NAMES:
        section = entry.section(name)
        print(f"  {name}:")
        for index, claim in enumerate(section.claims):
            sources = "; ".join(
                f"interview {c.session_id}, turn {c.turn_index}"
                if isinstance(c, InterviewTurnCitation)
                else f"artifact {c.artifact_id}"
                for c in claim.citations
            )
            print(f"    - {claim.text} [{sources}]")
            if (name, index) in doubts:
                print("      ! not in the cited answer: " + ", ".join(doubts[name, index]))
        if section.open_question is not None:
            print(f"    Open: {section.open_question}")


def _review_entries(
    books: RunbookService,
    session: UUID,
    plan: UUID,
    by: str,
    *,
    include_approved: bool = False,
    entry_ids: set[str] | None = None,
    review_by: date | None = None,
) -> int:
    drafts = [
        e
        for e in books.list(session)
        if (include_approved or e.entry.status == EntryStatus.DRAFT)
        and (entry_ids is None or e.entry.id in entry_ids)
    ]
    if not drafts:
        print("No draft entries to review.")
        return 0
    for stored in drafts:
        entry_id = UUID(stored.entry.id)
        while True:
            stored = books.get(entry_id)
            if not include_approved and stored.entry.status != EntryStatus.DRAFT:
                break
            _show_entry(stored, books.unsupported(session).get(stored.entry.id, ()))
            expected = {
                "expected_version": stored.version,
                "expected_maintenance_revision": books.metadata(entry_id)["maintenance_revision"],
            }
            try:
                action = (
                    input(
                        "Approve, review unchanged, withdraw, reject, edit, triage, skip, or quit? "
                    )
                    .strip()
                    .lower()
                )
                if action in ("quit", "q", "/quit"):
                    raise EOFError
                if action in ("skip", "s"):
                    break
                if action in ("approve", "a"):
                    books.approve(entry_id, by, **expected, review_by=review_by)
                    print("Approved.")
                    break
                if action == "triage":
                    principal = current_principal()
                    if principal is None:
                        raise AuthenticationError("review identity required")
                    feedback = FeedbackService(books.dsn, books.work_email_domains)
                    print(json.dumps(feedback.list(entry_id, principal), default=str))
                    flag_id = UUID(input("Flag ID: ").strip())
                    resolution = input(
                        "Resolution (corrected, confirmed_current, dismissed): "
                    ).strip()
                    feedback.resolve(
                        flag_id,
                        principal,
                        resolution=resolution,
                        note=input("Private resolution reason: "),
                        **expected,
                    )
                    print("Flag resolved.")
                    continue
                if action in ("review", "review unchanged"):
                    books.review(entry_id, by, **expected, review_by=review_by)
                    print("Reviewed; no content changes.")
                    break
                if action == "withdraw":
                    books.withdraw(entry_id, by, input("Reason: "), **expected)
                    print("Withdrawn to draft.")
                    break
                if action in ("reject", "r"):
                    books.reject(entry_id, by, input("Reason: "), **expected)
                    print("Rejected.")
                    break
                if action in ("edit", "e"):
                    name = input("Section: ").strip()
                    if name not in SECTION_NAMES:
                        print("Choose a section: " + ", ".join(SECTION_NAMES))
                        continue
                    claims = []
                    while text := input("Claim (blank to finish): ").strip():
                        claims.append(text)
                    question = input("Open question (blank for none): ").strip() or None
                    books.edit(
                        entry_id,
                        by,
                        {name: SectionEdit(claims=tuple(claims), open_question=question)},
                        **expected,
                    )
                    print("Edited; approval is required.")
                    continue
                print(
                    "Choose approve, review unchanged, withdraw, reject, edit, triage, skip, "
                    "or quit."
                )
            except (EOFError, KeyboardInterrupt):
                print(f"Stopped. Continue with: eki review --plan {plan}")
                return 0
            except (ValueError, ConflictError) as error:
                # Validation can contain rejected input. Never echo that input in the terminal.
                print(
                    str(error)
                    if isinstance(error, ConflictError)
                    else "Review refused: check the name, section and content."
                )
    print("Review complete.")
    return 0


def _entry_commands(arguments: argparse.Namespace) -> int:
    settings = get_settings()
    books = RunbookService(settings.database_url.get_secret_value(), work_domains(settings))
    if arguments.command == "review" and arguments.run is not None:
        credential = settings.api_token
        if credential is None:
            raise AuthenticationError("set EKI_API_TOKEN to review entries")
        principal = TokenAuthenticator(settings.api_tokens.get_secret_value()).authenticate(
            "Bearer " + credential.get_secret_value()
        )
        with acting_as(principal), audit_context(user_id=principal.subject):
            selected: list[dict[str, Any]] = []
            offset: int | None = 0
            with books._connect() as conn:
                while offset is not None:
                    page = maintenance.queue(
                        conn,
                        arguments.run,
                        principal,
                        filter="due" if arguments.due else "all",
                        limit=100,
                        offset=offset,
                    )
                    selected.extend(page["entries"])
                    offset = page["next_offset"]
            for sid in dict.fromkeys(e["session_id"] for e in selected):
                with books._connect() as conn:
                    session_row = interviews.load_session(conn, UUID(sid))
                if session_row:
                    _review_entries(
                        books,
                        UUID(sid),
                        session_row.plan_id,
                        principal.name,
                        include_approved=True,
                        entry_ids={e["id"] for e in selected if e["session_id"] == sid},
                        review_by=arguments.review_by,
                    )
        return 0
    with books._connect() as conn:
        plan = interviews.load_plan(conn, arguments.plan)
        if plan is None:
            raise NotFoundError(f"no plan {arguments.plan}")
        session = interviews.session_of_plan(conn, plan.id)
        if session is None:
            raise ConflictError("approve the plan and finish its interview first")
        row = conn.execute(
            "SELECT expert_name FROM ingest_runs WHERE id = %s", (plan.run_id,)
        ).fetchone()
        assert row is not None  # noqa: S101 - plan references this run
    if plan.kind == "follow_up":
        principal = followup_cli.principal(settings)
        followup_cli.require_manager(settings, principal, "plans", plan.id)
        print(f"Mapped proposals: eki follow-up proposals --session {session}")
    if arguments.command == "entries":
        entries = books.list(session)
        unsupported = books.unsupported(session)
        for stored in entries:
            entry = stored.entry
            doubts = len(unsupported.get(entry.id, ()))
            marker = f"  ({doubts} claim{'s' if doubts > 1 else ''} to check)" if doubts else ""
            marker += "  (edited by hand)" if stored.edited else ""
            print(f"{entry.id}  {entry.status.value:8}  {entry.title}{marker}")
        if not entries:
            print(
                "No new-topic draft entries; mapped changes are reviewed through proposals."
                if plan.kind == "follow_up"
                else "No entries yet. Finish the interview first."
            )
        return 0
    credential = settings.api_token
    if credential is None:
        raise AuthenticationError("set EKI_API_TOKEN to review entries")
    principal = TokenAuthenticator(settings.api_tokens.get_secret_value()).authenticate(
        "Bearer " + credential.get_secret_value()
    )
    with acting_as(principal), audit_context(user_id=principal.subject):
        with books._connect() as conn:
            maintenance.reviewer(conn, plan.run_id, principal.name)
        if arguments.review_as and arguments.review_as != principal.name:
            raise ValueError("--review-as cannot impersonate another reviewer")
        return _review_entries(
            books, session, plan.id, principal.name, review_by=arguments.review_by
        )


def main(argv: list[str] | None = None) -> int:
    with audit_context(user_id="local-operator", correlation_id=uuid4()):
        return _main(argv)


def _main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "follow-up":
            return followup_cli.execute(arguments, get_settings())
        if arguments.command in ("search", "ask", "export"):
            settings = get_settings()
            credential = settings.api_token
            if credential is None or not credential.get_secret_value():
                raise AuthenticationError("set EKI_API_TOKEN to a configured client credential")
            principal = TokenAuthenticator(settings.api_tokens.get_secret_value()).authenticate(
                "Bearer " + credential.get_secret_value()
            )
            if arguments.command == "export":
                exports = ExportService(
                    settings.database_url.get_secret_value(), work_domains(settings)
                )
                content = (
                    exports.entry(principal, arguments.entry)
                    if arguments.entry
                    else exports.run(principal, arguments.run)
                )
                if arguments.output is None:
                    print(content, end="")
                else:
                    try:
                        with arguments.output.open("x", encoding="utf-8") as output:
                            output.write(content)
                    except OSError:
                        return _fail(
                            "cannot create output file; check permissions "
                            "and that it does not exist"
                        )
                    print(f"Exported to {arguments.output}")
                return 0
            search = build_search(settings)
            if arguments.command == "ask":
                answer = asyncio.run(
                    build_answers(settings, search).ask(principal, arguments.question)
                )
                print(answer.model_dump_json() if arguments.json else answer.answer)
                if not arguments.json:
                    for evidence in answer.evidence:
                        print(
                            f"[{evidence.id}] {evidence.title}, {evidence.section}, "
                            f"version {evidence.version}"
                        )
                return 0
            hits = search.search(principal, arguments.query, limit=arguments.limit)
            if arguments.json:
                print(
                    json.dumps(
                        {
                            "results": [h.public() for h in hits],
                            "embedding_model": search.embedder.name,
                        }
                    )
                )
            else:
                for hit in hits:
                    print(f"{hit.entry.title} [{hit.entry.id}] ({hit.section})\n  {hit.snippet}")
                if not hits:
                    print("No matching approved entries.")
            return 0
        if arguments.command in ("entries", "review"):
            return _entry_commands(arguments)
        if arguments.command in ("plan", "interview"):
            return _interview_commands(arguments)
        with connect(get_settings()) as conn:
            if arguments.command == "users":
                return _users(conn, arguments)
            if arguments.command == "audit":
                print(
                    json.dumps(
                        list_auth_events(conn, after=arguments.after, limit=arguments.limit)
                        if arguments.auth
                        else list_events(
                            conn, arguments.run, after=arguments.after, limit=arguments.limit
                        )
                    )
                )
                return 0
            if arguments.command == "maintenance":
                operator = Principal("local-operator", "Local operator", True)
                print(
                    json.dumps(
                        {
                            "run": maintenance.state(conn, arguments.run, operator),
                            **maintenance.queue(
                                conn,
                                arguments.run,
                                operator,
                                filter=arguments.filter,
                                offset=arguments.offset,
                            ),
                        },
                        default=str,
                    )
                )
                return 0
            if arguments.command == "handover":
                state = maintenance.transfer(
                    conn,
                    arguments.run,
                    arguments.to,
                    Principal("local-operator", "Local operator", True),
                    expected_owner=arguments.expected_owner,
                    expected_revision=arguments.expected_run_revision,
                    reason=arguments.reason,
                    start=True,
                    identities=TokenAuthenticator(
                        get_settings().api_tokens.get_secret_value()
                    ).identities,
                )
                print(json.dumps(state, default=str))
                return 0
            if arguments.command == "access":
                operator = Principal("local-operator", "Local operator", True)
                if arguments.owner is not None:
                    access.set_owner(
                        conn,
                        arguments.run,
                        arguments.owner,
                        operator,
                        expected_owner=arguments.expected_owner,
                        expected_revision=arguments.expected_run_revision,
                        reason=arguments.reason,
                        identities=TokenAuthenticator(
                            get_settings().api_tokens.get_secret_value()
                        ).identities,
                    )
                if arguments.readers is not None:
                    access.set_readers(conn, arguments.run, arguments.readers, operator)
                print(json.dumps(access.readers(conn, arguments.run, operator)))
                return 0
            if arguments.command == "migrate":
                return _migrate(conn)
            if arguments.command == "ingest":
                return _ingest(conn, arguments)
            if arguments.command == "reindex-artifacts":
                if not interviews.run_exists(conn, arguments.run):
                    raise NotFoundError("no completed run")
                with audit_context(run_ids=(arguments.run,)):
                    embedder = artifact_embedder(get_settings(), run_id=arguments.run)
                    count = index_run(conn, arguments.run, embedder)
                print(f"Indexed {count} artifact chunks with {embedder.name}.")
                return 0
            if arguments.command == "map":
                return _map(conn, arguments.run)
            return _gaps(conn, arguments)
    except AuditError:
        return _fail("audit storage unavailable; operation refused")
    except AuthenticationError:
        return _fail("valid EKI_API_TOKEN credentials are required", USAGE_ERROR)
    except ConfigError as error:
        return _fail(str(error), USAGE_ERROR)
    except accounts.Throttled as error:
        return _fail(
            f"follow-up request rate limit reached; retry after {error.retry_after} seconds"
        )
    except (IngestError, NotFoundError, ConflictError, PlanError, ValueError) as error:
        return _fail(str(error))
    except ModelError:
        return _fail("the model could not be reached; nothing was lost, run the command again")
    except psycopg.OperationalError:
        return _fail("cannot reach the database; is `docker compose up -d postgres` running?")


if __name__ == "__main__":
    sys.exit(main())
