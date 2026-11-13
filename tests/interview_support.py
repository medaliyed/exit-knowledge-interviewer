"""Shared set-up for workflow, interviewer and API tests: a small mapped run and fake agents."""

import json
import re
from collections.abc import Sequence
from typing import Any
from uuid import UUID

import psycopg
from langgraph.types import interrupt

from eki.graph.workflow import AgentNode, Deps, InterviewState, Limits
from eki.ingest.embedding import HashingEmbedder
from eki.ingest.pipeline import store as store_snapshot
from eki.ingest.redaction import RedactedText
from eki.interview import store
from eki.interview.interviewer import interview_topic
from eki.interview.models import SECTIONS
from eki.interview.service import Event, InterviewService
from eki.llm import ChatModel, Message, Reply, ToolSpec
from eki.mapping.models import DocStatus, GapScore, Topic, TopicKind
from eki.mapping.store import save_mapping
from tests import builders
from tests.fakes import ScriptedModel
from tests.support import WORK_DOMAINS

TOPIC_IDS = ("incident:billing:stale", "incident:ledger:drift", "procedure:gateway:flag")
_BLOCK = re.compile(r"<<<ARTIFACT (\S+) (\w+)\n(.*?)\n\2 END>>>", re.DOTALL)


def seed_run(conn: psycopg.Connection, *, mapped: bool = True) -> UUID:
    """Three tickets, one topic each, scored 90, 70 and 50."""
    bodies = (
        "Nadia Ferrand: Restarted the ledger-db replicas, then re-ran billing-batch.",
        "Nadia Ferrand: Applied the usual fix for the month-end drift.",
        "Nadia Ferrand: Toggled the partner flag before the release.",
    )
    artifacts = [
        builders.ticket(f"TCK-000{n + 1}", system, f"Ticket about {system}").model_copy(
            update={"body": RedactedText(body)}
        )
        for n, (system, body) in enumerate(
            zip(("billing", "ledger", "gateway"), bodies, strict=True)
        )
    ]
    run_id = store_snapshot(conn, builders.snapshot(artifacts))
    if mapped:
        topics = tuple(
            Topic(
                id=topic_id,
                kind=TopicKind(topic_id.split(":")[0]),
                system=topic_id.split(":")[1],
                label=f"Topic {n + 1}",
                artifact_ids=(artifacts[n].id,),
            )
            for n, topic_id in enumerate(TOPIC_IDS)
        )
        scores = tuple(
            GapScore(
                topic_id=topic.id,
                exclusivity=1,
                doc_gap=1,
                recency=1,
                criticality=1,
                score=90.0 - 20 * n,
                doc_status=DocStatus.NONE,
                linked_documents=(),
                explanation={"exclusivity": "Only the expert worked on this."},
            )
            for n, topic in enumerate(topics)
        )
        save_mapping(conn, run_id, topics, scores)
    return run_id


def quoting_drafter(messages: Sequence[Message], tools: Sequence[ToolSpec]) -> Reply:
    """A drafter policy: one question per topic, quoting the first artifact it was shown."""
    match = _BLOCK.search(messages[1].content)
    if match is None:
        return Reply(text=json.dumps({"questions": []}))
    artifact_id, _, body = match.groups()
    quote = body.split("\n")[-1][:60]
    question = {
        "text": f"What is behind {artifact_id}?",
        "artifact_id": artifact_id,
        "quote": quote,
    }
    return Reply(text=json.dumps({"questions": [question]}))


async def one_question_agent(deps: Deps, state: InterviewState) -> dict[str, Any]:
    """Stands in for the interviewer: one question per topic, then every section is noted.

    Pass ``agent=None`` to ``make_service`` to get the real interviewer instead.
    """
    session_id, topic_id = UUID(str(state["session_id"])), str(state["topic_id"])
    step = state.get("step", 0) + 1
    pending = state.get("pending")
    if pending is not None:
        answer = interrupt(pending)
        for section in SECTIONS:
            store.record_note(
                deps.conn, session_id, topic_id, section, RedactedText("noted"), turn=answer["turn"]
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


def make_service(
    dsn: str,
    *,
    agent: AgentNode | None = one_question_agent,
    drafter: ChatModel | None = None,
    interviewer: ChatModel | None = None,
    limits: Limits | None = None,
) -> InterviewService:
    return InterviewService(
        dsn=dsn,
        drafter=drafter or ScriptedModel(quoting_drafter, name="drafter"),
        interviewer=interviewer or ScriptedModel([], name="interviewer"),
        embedder=HashingEmbedder(),
        work_email_domains=WORK_DOMAINS,
        limits=limits or Limits(),
        agent=agent or interview_topic,
    )


async def say(
    service: InterviewService, session_id: UUID, text: str, **options: Any
) -> list[Event]:
    """Post one message and collect the events of the step it triggers."""
    return [event async for event in await service.post(session_id, text=text, **options)]
