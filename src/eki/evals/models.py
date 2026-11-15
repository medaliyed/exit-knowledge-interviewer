"""Scenario and golden files, and what a graded run records.

A scenario says which corpus, which expert and which planted topic to interview,
and how the simulated expert answers. A golden says what a good interview of
that topic must capture. Both are strict: an unknown key or section is an
error that names the file, so a typo in a human-owned file cannot pass silently.
"""

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from eki.interview.models import SECTIONS
from eki.runbook.models import SectionName


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Expert(_Model):
    name: str
    email: str
    work_domains: tuple[str, ...]


class Span(_Model):
    since: date
    until: date

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.until < self.since:
            raise ValueError("until must be on or after since")
        return self


class Script(_Model):
    """How the simulated expert answers: everything volunteered first, then follow-ups."""

    initial: dict[SectionName, str | None]
    follow_ups: dict[SectionName, str] = Field(default_factory=dict)
    freeform: str | None = None
    """When set, the expert says only this, whatever is asked."""

    @model_validator(mode="after")
    def _check(self) -> Self:
        missing = [s for s in SECTIONS if s not in self.initial]
        if missing:
            raise ValueError(f"initial needs every section; missing {', '.join(missing)}")
        return self


class Scenario(_Model):
    id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    title: str
    corpus: str
    """Path of the synthetic corpus, relative to the repository root."""
    expert: Expert
    span: Span
    topic_artifact_ids: tuple[str, ...] = Field(min_length=1)
    """The planted topic: the plan topic holding at least 80% of these is interviewed."""
    script: Script
    question_cap: int = Field(default=30, ge=1)
    """Hard stop for the answering loop, whatever the golden allows."""


class Golden(_Model):
    scenario: str
    noted: dict[SectionName, tuple[str, ...]] = Field(default_factory=dict)
    """Per section, phrases a note of that section must contain (case-insensitive)."""
    open: tuple[SectionName, ...] = ()
    """Sections that must be left open: an open item and no note."""
    follow_ups: tuple[SectionName, ...] = ()
    """Sections that must be asked about after the topic's first question."""
    mentions: tuple[tuple[str, ...], ...] = ()
    """Groups of artifact IDs that must appear together in one question."""
    max_questions: int = Field(ge=1)
    max_unsupported_terms: int = Field(default=2, ge=0)
    """A note with more terms than this that the expert never said counts as invented."""
    min_facts: float = Field(default=1.0, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _check(self) -> Self:
        both = [s for s in self.open if s in self.noted]
        if both:
            raise ValueError(f"a section cannot be both noted and open: {', '.join(both)}")
        if any(not group for group in self.mentions):
            raise ValueError("an empty mentions group cannot be satisfied")
        return self


class Check(_Model):
    name: str
    found: int
    expected: int
    passed: bool
    details: tuple[str, ...] = ()
    """What was missing or wrong, in the words of the transcript."""


class TranscriptTurn(_Model):
    ordinal: int
    speaker: str
    kind: str
    section: str | None
    text: str


class NoteRecord(_Model):
    section: str
    text: str
    turn: int


class OpenRecord(_Model):
    section: str
    reason: str


class ScenarioResult(_Model):
    scenario: str
    passed: bool
    finished: bool
    aborted: bool = False
    checks: tuple[Check, ...]
    questions: int
    transcript: tuple[TranscriptTurn, ...] = ()
    notes: tuple[NoteRecord, ...] = ()
    open_items: tuple[OpenRecord, ...] = ()
    model_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    seconds: float = 0.0
    error: str | None = None
    """Why the scenario did not complete: a budget stop or an exception type."""


class Report(_Model):
    run_at: datetime
    revision: str | None
    models: dict[str, str]
    settings: dict[str, Any] = Field(default_factory=dict)
    scenarios: tuple[ScenarioResult, ...]

    @property
    def passed(self) -> int:
        return sum(1 for s in self.scenarios if s.passed)


def _load[T: BaseModel](path: Path, model: type[T]) -> T:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return model.model_validate(data)
    except (ValidationError, ValueError) as error:
        raise ValueError(f"{path.name}: {error}") from error


def load_scenario(path: Path) -> Scenario:
    return _load(path, Scenario)


def load_golden(path: Path) -> Golden:
    return _load(path, Golden)
