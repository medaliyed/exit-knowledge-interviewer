"""``eki plan`` and ``eki interview``: draft a plan and run the interview in a terminal."""

import argparse
import asyncio
import json
from typing import Any
from uuid import UUID, uuid4

from eki import followup_cli
from eki.audit import (
    audit_context,
)
from eki.auth import (
    AuthenticationError,
    acting_as,
    current_principal,
)
from eki.cli.common import fail
from eki.cli.pipeline import topic_name
from eki.config import Settings
from eki.db import connect
from eki.interview import generation
from eki.interview import store as interviews
from eki.interview.models import InterviewPlan
from eki.interview.service import (
    ConflictError,
    Event,
    GenerationFailed,
    InterviewService,
    NotFoundError,
    SessionView,
    build_service,
)
from eki.llm.tracing import flush_tracer


def _show_plan(plan: InterviewPlan) -> None:
    print(f"Plan {plan.id} ({plan.status}), {len(plan.topics)} topics")
    if plan.kind == "follow_up" and plan.limits is not None:
        print(
            f"Follow-up from session {plan.parent_session_id}; revision {plan.revision}. "
            f"Limits: {plan.limits.questions} questions, {plan.limits.model_calls} model calls, "
            f"{plan.limits.embedding_calls} embedding calls."
        )
    for position, topic in enumerate(plan.topics, start=1):
        print(f"{position:>3}. {topic_name(topic.system, topic.label)}  ({topic.reason})")
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
        return fail("Drafting paused; completed topics are saved. Use the resume command above.")
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
            return fail("nobody to approve the plan: set EKI_EXPERT_NAME or --approve-as", 2)
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


def execute(arguments: argparse.Namespace, settings: Settings) -> int:
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
