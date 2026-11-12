"""The runbook entry template (F7, F9).

One template for every entry: seven sections, each a list of claims. A claim is
a single statement a reader may act on, and it carries the citations that
support it. A section the expert could not answer is recorded as an open
question; it is never filled in by invention.
"""

from enum import StrEnum
from typing import Annotated, Literal, Self, cast, get_args

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

NonBlank = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]

SectionName = Literal[
    "trigger", "diagnosis", "steps", "rollback", "contacts", "pitfalls", "rationale"
]
SECTION_NAMES: tuple[SectionName, ...] = get_args(SectionName)


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ArtifactCitation(_Model):
    """Points at an ingested artifact: a ticket, commit, PR, email or wiki page."""

    kind: Literal["artifact"] = "artifact"
    artifact_id: NonBlank
    quote: str | None = None


class InterviewTurnCitation(_Model):
    """Points at one turn of an interview session with the expert."""

    kind: Literal["interview_turn"] = "interview_turn"
    session_id: NonBlank
    turn_index: int = Field(ge=0)


Citation = Annotated[ArtifactCitation | InterviewTurnCitation, Field(discriminator="kind")]


class Claim(_Model):
    text: NonBlank
    citations: tuple[Citation, ...] = ()


class Section(_Model):
    claims: tuple[Claim, ...] = ()
    """In order. For ``steps`` the order is the procedure."""
    open_question: NonBlank | None = None
    """What the expert could not answer. Set instead of guessing."""


class EntryStatus(StrEnum):
    DRAFT = "draft"
    APPROVED = "approved"
    REJECTED = "rejected"


class RunbookEntry(_Model):
    id: NonBlank
    topic_id: NonBlank
    title: NonBlank
    summary: str = ""
    status: EntryStatus = EntryStatus.DRAFT

    trigger: Section = Section()
    diagnosis: Section = Section()
    steps: Section = Section()
    rollback: Section = Section()
    contacts: Section = Section()
    pitfalls: Section = Section()
    rationale: Section = Section()

    approved_by: NonBlank | None = None
    approved_at: AwareDatetime | None = None

    def section(self, name: SectionName) -> Section:
        return cast(Section, getattr(self, name))

    def missing_sections(self) -> tuple[SectionName, ...]:
        """Sections with neither a claim nor an open question: the template is incomplete."""
        return tuple(
            name
            for name in SECTION_NAMES
            if not self.section(name).claims and self.section(name).open_question is None
        )

    @model_validator(mode="after")
    def _check_approval(self) -> Self:
        if self.status is EntryStatus.APPROVED:
            if self.approved_by is None:
                raise ValueError("an approved entry needs approved_by")
            if self.approved_at is None:
                raise ValueError("an approved entry needs approved_at")
        elif self.approved_by is not None or self.approved_at is not None:
            raise ValueError(
                f"a {self.status.value} entry must not carry approved_by or approved_at"
            )
        return self
