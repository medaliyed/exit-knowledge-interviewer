"""Build the interview plan and let the expert change it until it is approved (F6).

Every function returns a new plan; an approved plan refuses all of them.
"""

import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from uuid import UUID, uuid4

from eki.ingest.redaction import RedactedText
from eki.interview.models import FollowUpLimits, InterviewPlan, PlannedTopic, Question
from eki.mapping.models import GapScore, Topic

DEFAULT_LIMIT = 10


class PlanError(Exception):
    """The requested change does not fit the plan."""


class PlanFrozenError(PlanError):
    """The plan is approved and can no longer change."""


def _editable(plan: InterviewPlan) -> None:
    if plan.status != "draft":
        raise PlanFrozenError("the plan is approved and can no longer change")


def _with_topics(plan: InterviewPlan, topics: Sequence[PlannedTopic]) -> InterviewPlan:
    return plan.model_copy(update={"topics": tuple(topics)})


def build_plan(
    run_id: UUID,
    topics: Sequence[Topic],
    scores: Sequence[GapScore],
    *,
    limit: int = DEFAULT_LIMIT,
    contradicting: Sequence[str] = (),
    plan_id: UUID | None = None,
) -> InterviewPlan:
    """The ``limit`` highest-scoring topics, then any other topic whose sources disagree."""
    by_id = {topic.id: topic for topic in topics}
    ranked = sorted(scores, key=lambda s: (-s.score, s.topic_id.encode()))
    chosen = [(s, "ranked") for s in ranked[:limit]]
    chosen += [(s, "contradiction") for s in ranked[limit:] if s.topic_id in set(contradicting)]
    planned = [
        PlannedTopic(
            topic_id=score.topic_id,
            label=by_id[score.topic_id].label,
            kind=by_id[score.topic_id].kind.value,
            system=by_id[score.topic_id].system,
            origin=origin,
            score=score.score,
            reason=(
                f"gap score {score.score:.1f}"
                if origin == "ranked"
                else "its sources contradict each other"
            ),
            artifact_ids=by_id[score.topic_id].artifact_ids,
        )
        for score, origin in chosen
        if score.topic_id in by_id
    ]
    return InterviewPlan(id=plan_id or uuid4(), run_id=run_id, topics=tuple(planned))


def with_questions(
    plan: InterviewPlan,
    topic_id: str,
    questions: Sequence[Question],
    dropped: Sequence[str] = (),
) -> InterviewPlan:
    _editable(plan)
    if plan.topic(topic_id) is None:
        raise PlanError(f"{topic_id} is not in the plan")
    return _with_topics(
        plan,
        [
            t.model_copy(update={"questions": tuple(questions), "dropped": tuple(dropped)})
            if t.topic_id == topic_id
            else t
            for t in plan.topics
        ],
    )


def remove_topic(plan: InterviewPlan, topic_id: str) -> InterviewPlan:
    _editable(plan)
    if plan.topic(topic_id) is None:
        raise PlanError(f"{topic_id} is not in the plan")
    return _with_topics(plan, [t for t in plan.topics if t.topic_id != topic_id])


def edit_follow_up(
    plan: InterviewPlan,
    *,
    seeds: Mapping[str, RedactedText],
    remove_gaps: Sequence[str],
    limits: FollowUpLimits | None = None,
) -> InterviewPlan:
    """Review selected questions and visible limits without changing their provenance."""
    _editable(plan)
    if plan.kind != "follow_up":
        raise PlanError("follow-up question edits require a follow-up plan")
    selected = {q.gap_id for t in plan.topics for q in t.questions}
    if not set(seeds).union(remove_gaps) <= selected:
        raise PlanError("a question is not in the selected follow-up gaps")
    if any(not text.strip() or len(text) > 2000 for text in seeds.values()):
        raise PlanError("a seed question needs 1 to 2000 characters")
    topics = []
    for topic in plan.topics:
        questions = tuple(
            q.model_copy(update={"text": seeds[q.gap_id]})
            if q.gap_id is not None and q.gap_id in seeds
            else q
            for q in topic.questions
            if q.gap_id not in remove_gaps
        )
        if questions:
            sections = (
                None
                if topic.sections is None
                else tuple(s for s in topic.sections if any(q.section == s for q in questions))
            )
            topics.append(topic.model_copy(update={"questions": questions, "sections": sections}))
    return plan.model_copy(update={"topics": tuple(topics), "limits": limits or plan.limits})


def reorder(plan: InterviewPlan, topic_ids: Sequence[str]) -> InterviewPlan:
    _editable(plan)
    current = {t.topic_id: t for t in plan.topics}
    if len(topic_ids) != len(current) or set(topic_ids) != set(current):
        raise PlanError("a new order must list exactly the topics of the plan, each once")
    return _with_topics(plan, [current[topic_id] for topic_id in topic_ids])


def add_topic(
    plan: InterviewPlan,
    label: RedactedText,
    note: RedactedText,
    *,
    position: int | None = None,
) -> InterviewPlan:
    """Add a topic the expert thinks is missing, with one question in their own words."""
    _editable(plan)
    if not label.strip() or not note.strip():
        raise PlanError("an added topic needs a label and a note on what to ask")
    index = len(plan.topics) if position is None else position
    if not 0 <= index <= len(plan.topics):
        raise PlanError(f"position must be between 0 and {len(plan.topics)}")
    slug = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")[:40] or "topic"
    topic_id, suffix = f"expert:{slug}", 1
    while plan.topic(topic_id) is not None:
        suffix += 1
        topic_id = f"expert:{slug}-{suffix}"
    added = PlannedTopic(
        topic_id=topic_id,
        label=label.strip(),
        kind="expert",
        system=None,
        origin="expert",
        score=None,
        reason="added by the expert",
        artifact_ids=(),
        questions=(Question(text=RedactedText(note.strip()), origin="expert"),),
    )
    return _with_topics(plan, [*plan.topics[:index], added, *plan.topics[index:]])


def approve(plan: InterviewPlan, by: str, at: datetime) -> InterviewPlan:
    """Freeze the plan. After this the interview can start and no edit is accepted."""
    _editable(plan)
    if not plan.topics:
        raise PlanError("an empty plan cannot be approved")
    if not by.strip():
        raise PlanError("approval needs an approver")
    return plan.model_copy(
        update={"status": "approved", "approved_by": by.strip(), "approved_at": at}
    )
