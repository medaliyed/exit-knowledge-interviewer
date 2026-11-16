"""F22 terminal surfaces using the same authenticated follow-up services as HTTP.

Read, triage and plan-review commands never configure a provider. JSON comparisons
retain source citations and expected revisions so apply/refresh stays explicit.
"""

import argparse
import asyncio
import json
from collections.abc import Sequence
from typing import Any, cast
from uuid import UUID, uuid4

import psycopg

from eki import access, maintenance
from eki.audit import audit_context
from eki.auth import AuthenticationError, Principal, TokenAuthenticator, acting_as
from eki.config import ConfigError, Settings, work_domains
from eki.errors import NotFoundError
from eki.followup.gaps import Action, GapService
from eki.followup.proposals import ApplyMode
from eki.followup.sessions import FollowUpService
from eki.ingest.embedding import HashingEmbedder
from eki.ingest.redaction import RedactedText
from eki.interview import plan as plan_review
from eki.interview import store
from eki.interview.models import FollowUpLimits
from eki.interview.service import InterviewService
from eki.llm import Message, Reply, ToolSpec
from eki.runbook.models import SECTION_NAMES, SectionName
from eki.runbook.service import RunbookService


def add_parser(commands: Any) -> None:
    root = commands.add_parser("follow-up", help="save, plan, interview and review follow-up work")
    actions = root.add_subparsers(dest="follow_up_command", required=True)

    def command(name: str, help_text: str) -> argparse.ArgumentParser:
        parser: argparse.ArgumentParser = actions.add_parser(name, help=help_text)
        parser.add_argument("--json", action="store_true", help="JSON output (the default)")
        return parser

    def run(parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--run", type=UUID, required=True)

    def gap(parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--gap", type=UUID, required=True)

    def plan(parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--plan", type=UUID, required=True)

    def revision(parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--expected-revision", type=int, required=True)

    def version(parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--expected-version", type=int, required=True)

    def limits(parser: argparse.ArgumentParser, *, defaults: bool) -> None:
        for name, default in (
            ("topics", 3),
            ("questions", 6),
            ("model-calls", 12),
            ("embedding-calls", 6),
        ):
            parser.add_argument(f"--max-{name}", type=int, default=default if defaults else None)

    save = command("save", "explicitly save a question to the chosen handover")
    run(save)
    save.add_argument("--question", required=True)
    save.add_argument("--request-id", type=UUID, default=None, help="reuse this UUID for retries")
    save.add_argument("--entry", type=UUID)
    save.add_argument("--section", choices=SECTION_NAMES)
    receipt = command("receipt", "read your own saved request receipt")
    gap(receipt)
    gaps = command("gaps", "inspect the owner's gap queue")
    run(gaps)
    gaps.add_argument(
        "--filter",
        default="open",
        choices=("open", "all", "deferred", "dismissed", "requests", "open_sections"),
    )
    gaps.add_argument("--limit", type=int, default=50)
    gaps.add_argument("--offset", type=int, default=0)
    triage = command("triage", "map, group, defer, dismiss or reopen a gap")
    run(triage)
    gap(triage)
    revision(triage)
    triage.add_argument(
        "--action",
        required=True,
        choices=("map", "new_topic", "defer", "dismiss", "reopen", "group"),
    )
    triage.add_argument("--target-entry", type=UUID)
    triage.add_argument("--target-section", choices=SECTION_NAMES)
    triage.add_argument("--seed-question")
    triage.add_argument("--priority", type=int)
    triage.add_argument("--reason")
    triage.add_argument("--topic-label")
    triage.add_argument("--group-with", type=UUID)
    prepare = command("plan", "prepare a focused plan without model calls")
    run(prepare)
    prepare.add_argument("--parent-session", type=UUID, required=True)
    prepare.add_argument("--gap", type=UUID, action="append", required=True)
    prepare.add_argument(
        "--request-id", type=UUID, default=None, help="reuse this UUID for retries"
    )
    limits(prepare, defaults=True)
    show = command("show-plan", "review a focused plan, selected questions and limits")
    plan(show)
    edit = command("edit-plan", "edit selected seed questions, remove gaps or change limits")
    plan(edit)
    revision(edit)
    edit.add_argument("--seed", action="append", default=[], metavar="GAP_ID=QUESTION")
    edit.add_argument("--remove-gap", type=UUID, action="append", default=[])
    limits(edit, defaults=False)
    discard = command("discard", "discard an unapproved follow-up and release its gaps")
    plan(discard)
    revision(discard)
    proposals = command("proposals", "compare unpublished proposals from a finished follow-up")
    proposals.add_argument("--session", type=UUID, required=True)
    proposal = command("proposal", "inspect one proposal's current comparison")
    proposal.add_argument("--proposal", type=UUID, required=True)
    apply = command("apply", "apply selected sections to draft; separate approval is required")
    apply.add_argument("--proposal", type=UUID, required=True)
    revision(apply)
    version(apply)
    apply.add_argument("--expected-maintenance-revision", type=int)
    apply.add_argument("--section", action="append", required=True, metavar="SECTION=merge|replace")
    refresh = command("refresh", "explicitly refresh a stale proposal after reviewing its target")
    refresh.add_argument("--proposal", type=UUID, required=True)
    revision(refresh)
    version(refresh)
    resolve = command("resolve", "confirm an approved entry/version answers a saved question")
    run(resolve)
    gap(resolve)
    revision(resolve)
    version(resolve)
    resolve.add_argument("--entry", type=UUID, required=True)
    resolve.add_argument("--confirms-answer", action="store_true", required=True)
    stop = command("stop", "stop further questions after settling accepted messages")
    stop.add_argument("--session", type=UUID, required=True)


def principal(settings: Settings) -> Principal:
    credential = settings.api_token
    if credential is None or not credential.get_secret_value():
        raise AuthenticationError("set EKI_API_TOKEN to a configured client credential")
    return TokenAuthenticator(settings.api_tokens.get_secret_value()).authenticate(
        "Bearer " + credential.get_secret_value()
    )


def require_manager(settings: Settings, actor: Principal, resource: str, identity: UUID) -> UUID:
    """Protect private direct-service reads just as API middleware does."""
    with psycopg.connect(settings.database_url.get_secret_value(), autocommit=True) as conn:
        maintenance.active(conn, actor)
        run_id = access.resource_run(conn, resource, identity)
        if run_id is None:
            raise NotFoundError("resource not found")
        access.require_manager(conn, run_id, actor)
        return run_id


class _NoModel:
    name = "not-configured"

    async def complete(self, messages: Sequence[Message], tools: Sequence[ToolSpec] = ()) -> Reply:
        raise ConfigError("plan review never calls a model")


def _offline_service(settings: Settings) -> InterviewService:
    return InterviewService(
        settings.database_url.get_secret_value(),
        _NoModel(),
        _NoModel(),
        HashingEmbedder(),
        work_domains(settings),
    )


def _limits(
    arguments: argparse.Namespace, previous: FollowUpLimits | None = None
) -> FollowUpLimits:
    data = (previous or FollowUpLimits()).model_dump()
    for name in ("topics", "questions", "model_calls", "embedding_calls"):
        value = getattr(arguments, f"max_{name}")
        if value is not None:
            data[name] = value
    return FollowUpLimits.model_validate(data)


def _sections(values: list[str]) -> dict[SectionName, ApplyMode]:
    result: dict[SectionName, ApplyMode] = {}
    for value in values:
        section, _, mode = value.partition("=")
        if section not in SECTION_NAMES or mode not in ("merge", "replace") or section in result:
            raise ValueError("choose each section once as SECTION=merge or SECTION=replace")
        result[section] = cast(ApplyMode, mode)
    return result


def execute(arguments: argparse.Namespace, settings: Settings) -> int:
    actor = principal(settings)
    with acting_as(actor), audit_context(user_id=actor.subject):
        result = _execute(arguments, settings, actor)
    print(json.dumps(result, default=str, indent=2))
    return 0


def _execute(arguments: argparse.Namespace, settings: Settings, actor: Principal) -> Any:
    dsn = settings.database_url.get_secret_value()
    domains = work_domains(settings)
    gaps, followups = GapService(dsn, domains), FollowUpService(dsn, domains)
    action = arguments.follow_up_command
    if action == "save":
        saved, created = gaps.save_request(
            arguments.run,
            actor,
            request_id=arguments.request_id or uuid4(),
            question=arguments.question,
            entry_id=arguments.entry,
            section=arguments.section,
        )
        return {"receipt": saved, "created": created}
    if action == "receipt":
        return gaps.receipt(arguments.gap, actor)
    if action == "gaps":
        return gaps.queue(
            arguments.run,
            actor,
            filter=arguments.filter,
            limit=arguments.limit,
            offset=arguments.offset,
        )
    if action == "triage":
        return gaps.triage(
            arguments.run,
            arguments.gap,
            actor,
            action=cast(Action, arguments.action),
            expected_revision=arguments.expected_revision,
            target_entry_id=arguments.target_entry,
            target_section=arguments.target_section,
            seed_question=arguments.seed_question,
            priority=arguments.priority,
            reason=arguments.reason,
            topic_label=arguments.topic_label,
            group_with=arguments.group_with,
        )
    if action == "resolve":
        return gaps.resolve(
            arguments.run,
            arguments.gap,
            actor,
            expected_revision=arguments.expected_revision,
            entry_id=arguments.entry,
            expected_version=arguments.expected_version,
            confirms_answer=arguments.confirms_answer,
        )
    if action == "plan":
        return followups.prepare(
            arguments.run,
            actor,
            request_id=arguments.request_id or uuid4(),
            parent_session_id=arguments.parent_session,
            gap_ids=arguments.gap,
            limits=_limits(arguments),
        ).model_dump(mode="json")
    if action == "discard":
        return followups.discard(
            arguments.plan, actor, expected_revision=arguments.expected_revision
        ).model_dump(mode="json")
    if action == "stop":
        return followups.stop(arguments.session, actor)
    if action in ("show-plan", "edit-plan"):
        require_manager(settings, actor, "plans", arguments.plan)
        service = _offline_service(settings)
        before = asyncio.run(service.get_plan(arguments.plan))
        if before.kind != "follow_up":
            raise ValueError("choose a follow-up plan")
        if action == "show-plan":
            return before.model_dump(mode="json")
        seeds: dict[str, RedactedText] = {}
        for seed in arguments.seed:
            key, _, question = seed.partition("=")
            key = str(UUID(key))
            if not question.strip() or key in seeds:
                raise ValueError("choose each seed once as GAP_ID=QUESTION")
            seeds[key] = RedactedText(question)
        changed = asyncio.run(
            service.edit_plan(
                before.id,
                lambda plan: plan_review.edit_follow_up(
                    plan,
                    seeds=seeds,
                    remove_gaps=[str(g) for g in arguments.remove_gap],
                    limits=_limits(arguments, before.limits),
                ),
                expected_revision=arguments.expected_revision,
            )
        )
        return changed.model_dump(mode="json")
    books = RunbookService(dsn, domains)
    if action == "proposals":
        require_manager(settings, actor, "sessions", arguments.session)
        return {
            "session_id": str(arguments.session),
            "proposals": books.proposals_for_session(arguments.session),
        }
    require_manager(settings, actor, "follow-up-proposals", arguments.proposal)
    if action == "proposal":
        return books.proposal(arguments.proposal)
    if action == "refresh":
        return books.refresh_proposal(
            arguments.proposal,
            actor.name,
            expected_revision=arguments.expected_revision,
            expected_version=arguments.expected_version,
        )
    stored, replayed = books.apply_proposal(
        arguments.proposal,
        actor.name,
        _sections(arguments.section),
        expected_revision=arguments.expected_revision,
        expected_version=arguments.expected_version,
        expected_maintenance_revision=arguments.expected_maintenance_revision,
    )
    with psycopg.connect(dsn, autocommit=True) as conn:
        session = store.load_session(conn, stored.session_id)
    return {
        "entry": {
            **stored.entry.model_dump(mode="json"),
            "version": stored.version,
            "session_id": str(stored.session_id),
            "plan_id": str(session.plan_id) if session else None,
        },
        "proposal": books.proposal(arguments.proposal),
        "replayed": replayed,
    }
