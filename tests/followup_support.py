"""A finished interview with one public open section and one confidential section (F22)."""

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from langgraph.types import interrupt

from eki import access
from eki.graph.workflow import Deps, InterviewState
from eki.ingest.redaction import RedactedText
from eki.interview import store
from eki.interview.models import SECTIONS
from eki.runbook.service import RunbookService
from tests.conftest import Scratch
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
