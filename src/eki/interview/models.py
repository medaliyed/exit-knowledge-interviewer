"""The interview plan, the transcript, and what was learned per runbook section.

Every text field is redacted text. Turning notes into cited runbook entries is
phase 3; this phase only records them with the turn they came from.
"""

from collections.abc import Mapping
from datetime import datetime
from typing import Any, Literal, Self, get_args
from uuid import UUID

from pydantic import BaseModel, ConfigDict, model_validator

from eki.ingest.redaction import RedactedText
from eki.runbook.models import SectionName

SECTIONS: tuple[SectionName, ...] = get_args(SectionName)

SectionStatus = Literal["noted", "confidential", "open", "missing"]
"""``confidential``: answered, but the answer may not be shown to a model or published."""


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    def model_copy(self, *, update: Mapping[str, Any] | None = None, deep: bool = False) -> Self:
        """Copy, validating any update (pydantic's own ``update`` skips validation)."""
        if not update:
            return super().model_copy(deep=deep)
        return type(self).model_validate({**dict(self), **update})


class Question(_Model):
    text: RedactedText
    origin: Literal["drafted", "expert"] = "drafted"
    artifact_id: str | None = None
    quote: RedactedText | None = None
    """Verbatim from the artifact. Required for a drafted question (F6)."""

    @model_validator(mode="after")
    def _check(self) -> Self:
        if not self.text.strip():
            raise ValueError("a question needs text")
        if self.origin == "drafted" and not (self.artifact_id and (self.quote or "").strip()):
            raise ValueError("a drafted question must quote an artifact")
        return self


class PlannedTopic(_Model):
    topic_id: str
    label: str
    kind: str
    system: str | None
    origin: Literal["ranked", "contradiction", "expert"]
    """Why it is in the plan: its gap score, sources that disagree, or the expert added it."""
    score: float | None
    reason: str
    artifact_ids: tuple[str, ...]
    questions: tuple[Question, ...] = ()
    dropped: tuple[str, ...] = ()
    """Why drafted questions were thrown away, for example a quote that is not in the artifact."""


class InterviewPlan(_Model):
    id: UUID
    run_id: UUID
    status: Literal["draft", "approved"] = "draft"
    topics: tuple[PlannedTopic, ...]
    """In interview order."""
    approved_by: str | None = None
    approved_at: datetime | None = None

    @model_validator(mode="after")
    def _check(self) -> Self:
        ids = [topic.topic_id for topic in self.topics]
        if len(ids) != len(set(ids)):
            raise ValueError("a topic appears more than once in the plan")
        if self.status == "approved":
            if not (self.approved_by and self.approved_at):
                raise ValueError("an approved plan needs an approver and a time")
            if not self.topics:
                raise ValueError("an empty plan cannot be approved")
        return self

    def topic(self, topic_id: str) -> PlannedTopic | None:
        return next((t for t in self.topics if t.topic_id == topic_id), None)


class Session(_Model):
    id: UUID
    plan_id: UUID
    run_id: UUID
    status: Literal["active", "finished"]
    current_topic_id: str | None


class Turn(_Model):
    author_subject: str | None = None
    author_name: str | None = None
    ordinal: int
    """Position in the session's transcript, from 1."""
    topic_id: str | None
    speaker: Literal["interviewer", "expert", "maintainer"]
    kind: Literal["question", "answer", "skip", "correction", "edit"]
    text: RedactedText
    confidential: bool = False
    section: SectionName | None = None
    """Question turns: the section the question is about."""
    answers: int | None = None
    """Answer and skip turns: the question turn this responds to."""
    corrects: int | None = None
    """Correction turns: the earlier expert turn this replaces."""

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.speaker == "maintainer" and (
            self.kind != "edit"
            or self.confidential
            or not self.author_subject
            or not self.author_name
        ):
            raise ValueError("maintainer edits need attributed public provenance")
        if self.speaker == "interviewer" and (self.kind != "question" or self.confidential):
            raise ValueError("the interviewer only asks questions, and never confidentially")
        if self.speaker == "expert" and self.kind == "question":
            raise ValueError("an expert turn is an answer, a skip or a correction")
        if self.kind == "correction" and self.corrects is None:
            raise ValueError("a correction must say which turn it corrects")
        return self


class Note(_Model):
    topic_id: str
    section: SectionName
    text: RedactedText
    turn: int
    """The expert turn the note was taken from."""
    confidential: bool = False
    superseded: bool = False


class OpenItem(_Model):
    topic_id: str
    section: SectionName
    reason: RedactedText
    turn: int | None
    """The expert turn that left it open, when there is one."""
