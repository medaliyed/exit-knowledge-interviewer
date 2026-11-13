"""Task F6: the expert can prune, reorder and extend the plan until it is approved."""

from collections.abc import Callable
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from eki.ingest.redaction import RedactedText
from eki.interview.models import InterviewPlan
from eki.interview.plan import (
    PlanError,
    PlanFrozenError,
    add_topic,
    approve,
    build_plan,
    remove_topic,
    reorder,
    with_questions,
)
from eki.mapping.models import DocStatus, GapScore, Topic, TopicKind
from tests.test_interview_models import drafted

NOW = datetime(2026, 11, 2, 9, tzinfo=UTC)
RUN = uuid4()


def topics_and_scores(count: int) -> tuple[tuple[Topic, ...], tuple[GapScore, ...]]:
    topics = tuple(
        Topic(
            id=f"system:s:t{n:02d}",
            kind=TopicKind.SYSTEM,
            system="s",
            label=f"Topic {n}",
            artifact_ids=(f"TCK-{n:04d}",),
        )
        for n in range(count)
    )
    scores = tuple(
        GapScore(
            topic_id=t.id,
            exclusivity=1,
            doc_gap=1,
            recency=1,
            criticality=1,
            score=float(n * 3 % 17),
            doc_status=DocStatus.NONE,
            linked_documents=(),
            explanation={},
        )
        for n, t in enumerate(topics)
    )
    return topics, scores


def plan_of(count: int = 4) -> InterviewPlan:
    topics, scores = topics_and_scores(count)
    return build_plan(RUN, topics, scores)


def ids(plan: InterviewPlan) -> list[str]:
    return [t.topic_id for t in plan.topics]


def test_top_ten_by_score_in_order() -> None:
    topics, scores = topics_and_scores(14)
    plan = build_plan(RUN, topics, scores)
    by_score = sorted(scores, key=lambda s: (-s.score, s.topic_id))
    assert ids(plan) == [s.topic_id for s in by_score[:10]]
    assert plan.status == "draft"
    assert plan.run_id == RUN
    first = plan.topics[0]
    assert (first.origin, first.score, first.reason) == ("ranked", 16.0, "gap score 16.0")
    assert first.artifact_ids == ("TCK-0011",)
    assert first.questions == ()


def test_limit_and_contradicting_topics() -> None:
    topics, scores = topics_and_scores(14)
    lowest = min(scores, key=lambda s: (s.score, s.topic_id)).topic_id
    top = max(scores, key=lambda s: s.score).topic_id
    plan = build_plan(RUN, topics, scores, limit=3, contradicting=(lowest, top))
    assert len(plan.topics) == 4
    assert plan.topics[-1].topic_id == lowest
    assert plan.topics[-1].origin == "contradiction"
    assert plan.topics[-1].reason == "its sources contradict each other"
    assert plan.topic(top) is not None
    assert plan.topics[0].origin == "ranked", "a top topic stays where its score puts it"


def test_questions_are_attached_to_a_topic() -> None:
    plan = plan_of()
    target = ids(plan)[1]
    updated = with_questions(plan, target, (drafted(),), dropped=("quote not found in X",))
    changed = updated.topic(target)
    assert changed is not None
    assert changed.questions == (drafted(),)
    assert changed.dropped == ("quote not found in X",)
    with pytest.raises(PlanError, match="not in the plan"):
        with_questions(plan, "nope", ())


def test_removal_and_reorder() -> None:
    plan = plan_of()
    a, b, c, d = ids(plan)
    assert ids(remove_topic(plan, b)) == [a, c, d]
    assert ids(reorder(plan, [d, a, c, b])) == [d, a, c, b]
    with pytest.raises(PlanError, match="not in the plan"):
        remove_topic(plan, "nope")


def test_reorder_with_a_wrong_id_set_is_refused() -> None:
    plan = plan_of()
    a, b, c, d = ids(plan)
    for wrong in ([a, b, c], [a, b, c, d, "extra"], [a, a, b, c], [a, b, c, "other"]):
        with pytest.raises(PlanError, match="exactly the topics"):
            reorder(plan, wrong)


def test_an_added_topic_appears_where_requested() -> None:
    plan = plan_of()
    label, note = RedactedText("Audit export handover"), RedactedText("Ask me who signs it off.")
    at_end = add_topic(plan, label, note)
    added = at_end.topics[-1]
    assert added.topic_id == "expert:audit-export-handover"
    assert (added.origin, added.kind, added.score, added.artifact_ids) == (
        "expert",
        "expert",
        None,
        (),
    )
    (question,) = added.questions
    assert (question.text, question.origin, question.quote) == (note, "expert", None)
    assert ids(add_topic(plan, label, note, position=0))[0] == added.topic_id
    assert ids(add_topic(plan, label, note, position=2))[2] == added.topic_id
    twice = add_topic(at_end, label, note)
    assert twice.topics[-1].topic_id == "expert:audit-export-handover-2"
    for bad in (-1, 99):
        with pytest.raises(PlanError, match="position"):
            add_topic(plan, label, note, position=bad)
    with pytest.raises(PlanError, match="label"):
        add_topic(plan, RedactedText("  "), note)


def test_an_approved_plan_refuses_every_edit() -> None:
    approved = approve(plan_of(), "Nadia Ferrand", NOW)
    assert (approved.status, approved.approved_by, approved.approved_at) == (
        "approved",
        "Nadia Ferrand",
        NOW,
    )
    first = ids(approved)[0]
    text = RedactedText("x")
    edits: tuple[Callable[[], InterviewPlan], ...] = (
        lambda: remove_topic(approved, first),
        lambda: reorder(approved, ids(approved)),
        lambda: add_topic(approved, text, text),
        lambda: with_questions(approved, first, ()),
        lambda: approve(approved, "Someone else", NOW),
    )
    for edit in edits:
        with pytest.raises(PlanFrozenError):
            edit()


def test_an_empty_plan_cannot_be_approved() -> None:
    plan = plan_of(1)
    empty = remove_topic(plan, ids(plan)[0])
    with pytest.raises(PlanError, match="empty"):
        approve(empty, "Nadia Ferrand", NOW)
    with pytest.raises(PlanError, match="approver"):
        approve(plan, "  ", NOW)
