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
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import UUID, uuid4

import psycopg

from eki import accounts, followup_cli
from eki.audit import (
    AuditError,
    audit_context,
)
from eki.auth import (
    AuthenticationError,
)
from eki.cli import admin, interview, knowledge, pipeline, review
from eki.cli.common import USAGE_ERROR, fail
from eki.config import ConfigError, get_settings
from eki.db import connect, pending_warning
from eki.ingest.pipeline import IngestError
from eki.interview.plan import PlanError
from eki.interview.service import (
    ConflictError,
    NotFoundError,
)
from eki.llm import ModelError


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


def main(argv: list[str] | None = None) -> int:
    with audit_context(user_id="local-operator", correlation_id=uuid4()):
        return _main(argv)


def _warn_if_migrations_pending() -> None:
    """Tell the operator about unapplied migrations; the command itself still runs."""
    try:
        with connect(get_settings()) as conn:
            warning = pending_warning(conn)
    except (psycopg.Error, ConfigError):
        return  # the command reports an unreachable database or bad settings itself
    if warning is not None:
        print(f"eki: warning: {warning}", file=sys.stderr)


def _main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if arguments.command != "migrate":
        _warn_if_migrations_pending()
    try:
        if arguments.command == "follow-up":
            return followup_cli.execute(arguments, get_settings())
        if arguments.command in ("search", "ask", "export"):
            return knowledge.execute(arguments, get_settings())
        if arguments.command in ("entries", "review"):
            return review.execute(arguments, get_settings())
        if arguments.command in ("plan", "interview"):
            return interview.execute(arguments, get_settings())
        settings = get_settings()
        with connect(settings) as conn:
            if arguments.command == "users":
                return admin.cmd_users(conn, arguments, settings)
            if arguments.command in ("audit", "maintenance", "handover", "access"):
                return admin.execute(conn, arguments, settings)
            if arguments.command == "migrate":
                return pipeline.cmd_migrate(conn, settings)
            if arguments.command == "ingest":
                return pipeline.cmd_ingest(conn, arguments, settings)
            if arguments.command == "reindex-artifacts":
                return pipeline.cmd_reindex(conn, arguments, settings)
            if arguments.command == "map":
                return pipeline.cmd_map(conn, arguments.run)
            return pipeline.cmd_gaps(conn, arguments)
    except AuditError:
        return fail("audit storage unavailable; operation refused")
    except AuthenticationError:
        return fail("valid EKI_API_TOKEN credentials are required", USAGE_ERROR)
    except ConfigError as error:
        return fail(str(error), USAGE_ERROR)
    except accounts.Throttled as error:
        return fail(
            f"follow-up request rate limit reached; retry after {error.retry_after} seconds"
        )
    except (IngestError, NotFoundError, ConflictError, PlanError, ValueError) as error:
        return fail(str(error))
    except ModelError:
        return fail("the model could not be reached; nothing was lost, run the command again")
    except psycopg.OperationalError:
        return fail("cannot reach the database; is `docker compose up -d postgres` running?")


if __name__ == "__main__":
    sys.exit(main())
