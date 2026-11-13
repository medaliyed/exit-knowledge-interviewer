"""Task P2-b: the shapes of a plan, a transcript, notes and open items."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from eki.ingest.redaction import RedactedText
from eki.interview.models import (
    SECTIONS,
    InterviewPlan,
    Note,
    OpenItem,
    PlannedTopic,
    Question,
    Turn,
)


def text(value: str) -> RedactedText:
    return RedactedText(value)


def drafted(question: str = "Why replicas first?") -> Question:
    return Question(text=text(question), artifact_id="TCK-0006", quote=text("restarted replicas"))


def topic(topic_id: str = "incident:billing:stale", **changes: object) -> PlannedTopic:
    fields: dict[str, object] = {
        "topic_id": topic_id,
        "label": "Stale snapshot",
        "kind": "incident",
        "system": "billing-batch",
        "origin": "ranked",
        "score": 79.5,
        "reason": "gap score 79.5",
        "artifact_ids": ("TCK-0006",),
        "questions": (drafted(),),
    }
    return PlannedTopic.model_validate({**fields, **changes})


def test_sections_are_the_seven_runbook_sections() -> None:
    assert SECTIONS == (
        "trigger",
        "diagnosis",
        "steps",
        "rollback",
        "contacts",
        "pitfalls",
        "rationale",
    )


def test_drafted_question_needs_an_artifact_and_a_quote() -> None:
    assert drafted().origin == "drafted"
    for missing in ({"artifact_id": None}, {"quote": None}, {"quote": text("   ")}):
        fields = {"text": text("Why?"), "artifact_id": "TCK-1", "quote": text("a quote"), **missing}
        with pytest.raises(ValidationError):
            Question.model_validate(fields)


def test_expert_question_needs_no_quote_but_needs_text() -> None:
    assert Question(text=text("Ask me about the audit."), origin="expert").quote is None
    with pytest.raises(ValidationError):
        Question(text=text("  "), origin="expert")


def test_plan_keeps_topic_order_and_rejects_duplicates() -> None:
    first, second = topic("a"), topic("b")
    plan = InterviewPlan(id=uuid4(), run_id=uuid4(), topics=(second, first))
    assert [t.topic_id for t in plan.topics] == ["b", "a"]
    with pytest.raises(ValidationError, match="more than once"):
        InterviewPlan(id=uuid4(), run_id=uuid4(), topics=(first, first))


def test_approved_plan_needs_an_approver_a_time_and_a_topic() -> None:
    now = datetime(2026, 11, 1, tzinfo=UTC)
    base = {"id": uuid4(), "run_id": uuid4(), "status": "approved"}
    InterviewPlan.model_validate(
        {**base, "topics": (topic(),), "approved_by": "Nadia", "approved_at": now}
    )
    for broken in (
        {"topics": (topic(),), "approved_by": None, "approved_at": now},
        {"topics": (topic(),), "approved_by": "Nadia", "approved_at": None},
        {"topics": (), "approved_by": "Nadia", "approved_at": now},
    ):
        with pytest.raises(ValidationError):
            InterviewPlan.model_validate({**base, **broken})


def test_copy_with_update_is_validated() -> None:
    plan = InterviewPlan(id=uuid4(), run_id=uuid4(), topics=(topic(),))
    with pytest.raises(ValidationError):
        plan.model_copy(update={"status": "approved"})


def test_note_and_open_item_must_name_a_section() -> None:
    Note(topic_id="t", section="rollback", text=text("Restore the table."), turn=3)
    OpenItem(topic_id="t", section="contacts", reason=text("skipped by expert"), turn=None)
    with pytest.raises(ValidationError):
        Note.model_validate({"topic_id": "t", "section": "summary", "text": "x", "turn": 3})
    with pytest.raises(ValidationError):
        OpenItem.model_validate({"topic_id": "t", "section": "", "reason": "x", "turn": None})


def test_turn_rules() -> None:
    Turn(ordinal=1, topic_id="t", speaker="interviewer", kind="question", text=text("Why?"))
    Turn(
        ordinal=2, topic_id="t", speaker="expert", kind="answer", text=text("x"), confidential=True
    )
    for broken in (
        {"speaker": "interviewer", "kind": "answer"},
        {"speaker": "expert", "kind": "question"},
        {"speaker": "interviewer", "kind": "question", "confidential": True},
        {"speaker": "expert", "kind": "correction", "corrects": None},
    ):
        with pytest.raises(ValidationError):
            Turn.model_validate({"ordinal": 1, "topic_id": "t", "text": "x", **broken})
