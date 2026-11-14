"""The interview workflow: fixed nodes around ONE agentic node.

    draft_plan -> await_review -> next_topic -> interview_topic -> check_complete
                                      ^              |  ^               |
                                      |              +--+ (one question |
                                      +---------------- per step) ------+

``draft_plan``, ``await_review``, ``next_topic``, ``check_complete`` and the final
``synthesise`` node are plain code. Synthesis stores private drafts; expert review
is a separate database operation. ``interview_topic`` is the only node where a
model chooses tool calls.
The workflow stops at two kinds of interrupt: once for the expert to review the
plan, then once per question. State is checkpointed in Postgres after every
step, so a stopped process continues from the last question.

Workflow state holds control data and the model conversation of the current
topic. The record (plan, transcript, notes, open items) lives in the interview
tables; nodes write it in a way that is safe to repeat.
"""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Protocol, TypedDict
from uuid import UUID

import psycopg
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import interrupt

from eki.ingest.embedding import Embedder
from eki.ingest.pipeline import load as load_snapshot
from eki.interview import store
from eki.interview.conflicts import find_order_conflicts
from eki.interview.drafting import draft_questions
from eki.interview.plan import DEFAULT_LIMIT, build_plan, with_questions
from eki.llm import ChatModel
from eki.mapping.store import load_mapping
from eki.runbook.service import synthesise


class WorkflowError(Exception):
    """The workflow was asked to do something its state does not allow."""


class InterviewState(TypedDict, total=False):
    run_id: str
    plan_id: str
    session_id: str | None
    status: str
    """``review``, ``interviewing`` or ``done``."""
    topic_index: int
    topic_id: str | None
    messages: list[dict[str, Any]]
    """The interviewer's conversation with the model for the current topic. Redacted."""
    pending: dict[str, Any] | None
    """The question waiting for the expert: turn, question, section, call_id."""
    notices: list[str]
    """Things the interviewer must be told with the next tool result."""
    expert_turns: int
    model_calls: int
    last_turn: int | None
    """The expert turn a note may be taken from."""
    step: int
    topic_done: bool


@dataclass(frozen=True)
class Limits:
    plan_size: int = DEFAULT_LIMIT
    turns_per_topic: int = 12
    """Questions the interviewer may ask about one topic before the rest is marked open."""
    model_calls_per_topic: int = 40


@dataclass(frozen=True)
class Deps:
    """What the nodes need. One instance per operation; ``conn`` is used by one step at a time."""

    conn: psycopg.Connection
    drafter: ChatModel
    interviewer: ChatModel
    embedder: Embedder
    work_email_domains: frozenset[str]
    limits: Limits = field(default_factory=Limits)


AgentNode = Callable[[Deps, InterviewState], Awaitable[dict[str, Any]]]


class _BoundNode(Protocol):
    """A node as LangGraph wants it: one argument, named ``state``."""

    async def __call__(self, state: InterviewState) -> dict[str, Any]: ...


db = asyncio.to_thread
"""Run a blocking database call off the event loop."""


def thread_config(plan_id: UUID) -> RunnableConfig:
    """One workflow thread per plan."""
    return {"configurable": {"thread_id": str(plan_id)}, "recursion_limit": 500}


def emit(event: dict[str, Any]) -> None:
    """Send a progress event to whoever is streaming this step. A no-op otherwise."""
    get_stream_writer()(event)


async def forget_thread(dsn: str, plan_id: UUID) -> None:
    """Delete the checkpoints of one plan's workflow."""
    async with AsyncPostgresSaver.from_conn_string(dsn) as saver:
        await saver.adelete_thread(str(plan_id))


def setup_checkpoints(dsn: str) -> None:
    """Create the checkpoint tables. Safe to repeat."""
    with PostgresSaver.from_conn_string(dsn) as saver:
        saver.setup()


async def _draft_plan(deps: Deps, state: InterviewState) -> dict[str, Any]:
    run_id, plan_id = UUID(state["run_id"]), UUID(state["plan_id"])
    if await db(store.load_plan, deps.conn, plan_id) is not None:
        return {"status": "review"}
    snapshot = await db(load_snapshot, deps.conn, run_id)
    topics, scores = await db(load_mapping, deps.conn, run_id)
    if not scores:
        raise WorkflowError("the run has no gap scores yet: map it before planning")
    artifacts = {artifact.id: artifact for artifact in snapshot.artifacts}
    topic_by_id = {topic.id: topic for topic in topics}
    score_by_id = {score.topic_id: score for score in scores}
    conflicts = {
        topic.id: find_order_conflicts([artifacts[i] for i in topic.artifact_ids if i in artifacts])
        for topic in topics
    }
    plan = build_plan(
        run_id,
        topics,
        scores,
        limit=deps.limits.plan_size,
        contradicting=[topic_id for topic_id, found in conflicts.items() if found],
        plan_id=plan_id,
    )
    for planned in plan.topics:
        topic = topic_by_id[planned.topic_id]
        draft = await draft_questions(
            topic,
            score_by_id.get(topic.id),
            [artifacts[i] for i in topic.artifact_ids if i in artifacts],
            deps.drafter,
            work_email_domains=deps.work_email_domains,
            conflicts=conflicts[topic.id],
        )
        # A contradiction is asked about first, in words that quote both sides.
        questions = [conflict.question() for conflict in conflicts[topic.id]]
        questions += [q for q in draft.questions if q not in questions]
        plan = with_questions(plan, topic.id, questions, draft.dropped)
    await db(store.save_plan, deps.conn, plan)
    return {"status": "review"}


async def _await_review(deps: Deps, state: InterviewState) -> dict[str, Any]:
    decision = interrupt({"type": "plan_review", "plan_id": state["plan_id"]})
    plan = await db(store.load_plan, deps.conn, UUID(state["plan_id"]))
    if plan is None or plan.status != "approved":
        raise WorkflowError("the plan is not approved")
    session_id = await db(
        store.create_session, deps.conn, UUID(str(decision["session_id"])), plan.id
    )
    return {"session_id": str(session_id), "status": "interviewing", "topic_index": -1, "step": 0}


async def _next_topic(deps: Deps, state: InterviewState) -> dict[str, Any]:
    plan = await db(store.load_plan, deps.conn, UUID(state["plan_id"]))
    session_id = UUID(str(state["session_id"]))
    if plan is None:
        raise WorkflowError("the plan no longer exists")
    index = state["topic_index"] + 1
    fresh: dict[str, Any] = {
        "topic_index": index,
        "messages": [],
        "pending": None,
        "notices": [],
        "expert_turns": 0,
        "model_calls": 0,
        "last_turn": None,
        "topic_done": False,
    }
    if index >= len(plan.topics):
        return {**fresh, "topic_id": None, "status": "done"}
    topic_id = plan.topics[index].topic_id
    await db(store.set_current_topic, deps.conn, session_id, topic_id)
    emit(
        {
            "type": "topic",
            "topic_id": topic_id,
            "label": plan.topics[index].label,
            "index": index + 1,
            "total": len(plan.topics),
        }
    )
    return {**fresh, "topic_id": topic_id}


async def _check_complete(deps: Deps, state: InterviewState) -> dict[str, Any]:
    """Plain code: a topic is complete when each section has a note or is recorded as open."""
    checklist = await db(
        store.checklist, deps.conn, UUID(str(state["session_id"])), str(state["topic_id"])
    )
    missing = [section for section, status in checklist.items() if status == "missing"]
    if not missing:
        return {}
    notice = "The topic is not finished. These sections are still missing: " + ", ".join(missing)
    return {"topic_done": False, "notices": [*state.get("notices", []), notice]}


async def _synthesise(deps: Deps, state: InterviewState) -> dict[str, Any]:
    def work() -> None:
        with deps.conn.transaction():
            store.finish_session(deps.conn, UUID(str(state["session_id"])))
            synthesise(deps.conn, UUID(str(state["session_id"])))

    await db(work)
    return {"status": "done"}


def build_graph(
    deps: Deps, interview_topic: AgentNode
) -> StateGraph[InterviewState, None, InterviewState, InterviewState]:
    """The workflow, not yet compiled. ``interview_topic`` is the one agentic node."""
    agent = interview_topic

    def bound(node: AgentNode) -> _BoundNode:
        async def run(state: InterviewState) -> dict[str, Any]:
            return await node(deps, state)

        return run

    graph = StateGraph(InterviewState)
    graph.add_node("draft_plan", bound(_draft_plan))
    graph.add_node("await_review", bound(_await_review))
    graph.add_node("next_topic", bound(_next_topic))
    graph.add_node("interview_topic", bound(agent))
    graph.add_node("check_complete", bound(_check_complete))
    graph.add_node("synthesise", bound(_synthesise))
    graph.add_edge("synthesise", END)
    graph.add_edge(START, "draft_plan")
    graph.add_edge("draft_plan", "await_review")
    graph.add_edge("await_review", "next_topic")
    graph.add_conditional_edges(
        "next_topic",
        lambda state: "done" if state.get("status") == "done" else "interview",
        {"done": "synthesise", "interview": "interview_topic"},
    )
    graph.add_conditional_edges(
        "interview_topic",
        lambda state: "check" if state.get("topic_done") else "continue",
        {"check": "check_complete", "continue": "interview_topic"},
    )
    graph.add_conditional_edges(
        "check_complete",
        lambda state: "next" if state.get("topic_done") else "continue",
        {"next": "next_topic", "continue": "interview_topic"},
    )
    return graph


Workflow = CompiledStateGraph[InterviewState, None, InterviewState, InterviewState]


@asynccontextmanager
async def open_workflow(
    dsn: str, deps: Deps, interview_topic: AgentNode
) -> AsyncIterator[Workflow]:
    """The compiled workflow over the Postgres checkpointer, for the length of one operation."""
    async with AsyncPostgresSaver.from_conn_string(dsn) as saver:
        yield build_graph(deps, interview_topic).compile(checkpointer=saver)
