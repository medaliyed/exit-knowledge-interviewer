"""A finished interview with one public open section and one confidential section (F22)."""

from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from langgraph.types import interrupt

from eki import access
from eki.auth import Principal, acting_as
from eki.followup.gaps import GapService, open_section_gap_id
from eki.followup.sessions import FollowUpService
from eki.graph.workflow import Deps, InterviewState
from eki.ingest.redaction import RedactedText
from eki.interview import store
from eki.interview.models import SECTIONS, FollowUpLimits
from eki.runbook.service import RunbookService
from tests.conftest import Scratch
from tests.fakes import ScriptedModel, tool_reply
from tests.interview_support import TOPIC_IDS, make_service, say, seed_run
from tests.search_support import ADMIN, OWNER, READER

OPEN_REASON = "I never had to roll it back"


async def gap_agent(deps: Deps, state: InterviewState) -> dict[str, Any]:
    """One question per topic; the first topic leaves rollback open and pitfalls confidential."""
    session_id, topic_id = UUID(str(state["session_id"])), str(state["topic_id"])
    step = state.get("step", 0) + 1
    pending = state.get("pending")
    if pending is not None:
        answer = interrupt(pending)
        for section in SECTIONS:
            if topic_id == TOPIC_IDS[0] and section == "rollback":
                store.mark_open(
                    deps.conn,
                    session_id,
                    topic_id,
                    section,
                    RedactedText(OPEN_REASON),
                    turn=answer["turn"],
                )
            elif topic_id == TOPIC_IDS[0] and section == "pitfalls":
                store.record_note(
                    deps.conn,
                    session_id,
                    topic_id,
                    section,
                    store.CONFIDENTIAL_NOTE,
                    turn=answer["turn"],
                    confidential=True,
                )
            else:
                store.record_note(
                    deps.conn,
                    session_id,
                    topic_id,
                    section,
                    RedactedText(f"Noted {section} for {topic_id}."),
                    turn=answer["turn"],
                )
        return {"pending": None, "topic_done": True, "step": step}
    text = RedactedText(f"Tell me about {topic_id}.")
    turn = store.add_question(deps.conn, session_id, topic_id, text, section="trigger", step=step)
    return {
        "pending": {
            "type": "question",
            "turn": turn,
            "question": text,
            "section": "trigger",
            "topic_id": topic_id,
        },
        "step": step,
    }


@dataclass
class GapWorld:
    run_id: UUID
    session_id: UUID
    plan_id: UUID
    billing: UUID
    """Approved; rollback is an open public question, pitfalls answered confidentially."""
    ledger: UUID
    """Approved and complete."""
    gateway: UUID
    """Still a draft, complete."""


async def gap_world(scratch: Scratch) -> GapWorld:
    with scratch.connect() as conn:
        run = seed_run(conn)
    service = make_service(scratch.dsn, agent=gap_agent)
    plan = await service.draft_plan(run)
    session = (await service.approve(plan.id, OWNER.name)).session.id
    for _ in plan.topics:
        await say(service, session, "Recorded answer.")
    books = RunbookService(scratch.dsn)
    entries = books.list(session)
    for stored in entries[:2]:
        books.approve(UUID(stored.entry.id), OWNER.name)
    with scratch.connect() as conn:
        access.set_owner(conn, run, OWNER.subject, ADMIN)
        access.set_readers(conn, run, [READER.subject], OWNER)
    return GapWorld(run, session, plan.id, *(UUID(e.entry.id) for e in entries))


def ask(question: str, section: str, gap_id: str | None = None) -> tuple[str, dict[str, Any]]:
    arguments: dict[str, Any] = {"question": question, "section": section}
    if gap_id is not None:
        arguments["gap_id"] = gap_id
    return ("ask_expert", arguments)


def note(section: str, text: str) -> tuple[str, dict[str, Any]]:
    return ("record_note", {"section": section, "text": text})


async def triaged_world(scratch: Scratch) -> tuple[GapWorld, str, str]:
    """The gap world plus one reader question mapped to billing/contacts. Returns both gap IDs."""
    world = await gap_world(scratch)
    service = GapService(scratch.dsn)
    saved, _ = service.save_request(
        world.run_id,
        READER,
        request_id=uuid4(),
        question="Who authorises reopening a closed period?",
    )
    service.triage(
        world.run_id,
        saved["id"],
        OWNER,
        action="map",
        expected_revision=saved["revision"],
        target_entry_id=world.billing,
        target_section="contacts",
    )
    return world, str(open_section_gap_id(world.billing, "rollback")), saved["id"]


def prepare(
    scratch: Scratch,
    world: GapWorld,
    gap_ids: list[str],
    *,
    who: Principal = OWNER,
    request_id: UUID | None = None,
    limits: FollowUpLimits | None = None,
) -> Any:
    return FollowUpService(scratch.dsn).prepare(
        world.run_id,
        who,
        request_id=request_id or uuid4(),
        parent_session_id=world.session_id,
        gap_ids=gap_ids,
        limits=limits,
    )


ROLLBACK_ANSWER = "Restore the pre-run export."
CONTACT_ANSWERS = (
    "Finance operations lead authorises it.",
    "The CFO signs for amounts over the threshold.",
)


@dataclass
class AnsweredFollowUp:
    world: GapWorld
    session_id: UUID
    """The finished follow-up session."""
    rollback_gap: str
    request_gap: str


async def answered_follow_up(scratch: Scratch) -> AnsweredFollowUp:
    """A finished follow-up on billing: rollback answered once, contacts answered twice."""
    world, rollback_gap, request_gap = await triaged_world(scratch)
    return await answer_follow_up(scratch, world, rollback_gap, request_gap)


async def answer_follow_up(
    scratch: Scratch, world: GapWorld, rollback_gap: str, request_gap: str
) -> AnsweredFollowUp:
    """Prepare, approve and answer the billing follow-up of an already triaged world."""
    plan = prepare(scratch, world, [rollback_gap, request_gap])
    seed1, seed2 = (q.text for q in plan.topics[0].questions)
    model = ScriptedModel(
        [
            tool_reply(ask(seed1, "rollback", rollback_gap)),
            tool_reply(note("rollback", ROLLBACK_ANSWER), ask(seed2, "contacts", request_gap)),
            tool_reply(
                note("contacts", CONTACT_ANSWERS[0]),
                ask("Anyone else?", "contacts", request_gap),
            ),
            tool_reply(note("contacts", CONTACT_ANSWERS[1]), ("finish_topic", {})),
        ],
        name="interviewer",
    )
    interviews = make_service(scratch.dsn, agent=None, interviewer=model)
    with acting_as(OWNER):
        view = await interviews.approve(plan.id, OWNER.name)
        await say(interviews, view.session.id, ROLLBACK_ANSWER)
        await say(interviews, view.session.id, "Finance operations lead.")
        events = await say(interviews, view.session.id, "The CFO above the threshold.")
    assert events[-1]["type"] == "done", events
    return AnsweredFollowUp(world, view.session.id, rollback_gap, request_gap)
