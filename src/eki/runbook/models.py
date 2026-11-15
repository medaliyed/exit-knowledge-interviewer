"""The runbook entry template (F7, F9).

One template for every entry: seven sections, each a list of claims. A claim is
a single statement a reader may act on, and it carries the citations that
support it. A section the expert could not answer is recorded as an open
question; it is never filled in by invention.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Any, Literal, Self, cast, get_args

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    StringConstraints,
    model_serializer,
    model_validator,
)

NonBlank = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]

SectionName = Literal[
    "trigger", "diagnosis", "steps", "rollback", "contacts", "pitfalls", "rationale"
]
SECTION_NAMES: tuple[SectionName, ...] = get_args(SectionName)


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    def model_copy(self, *, update: Mapping[str, Any] | None = None, deep: bool = False) -> Self:
        """Copy, validating any update.

        Pydantic's own ``model_copy(update=...)`` skips validation, which would let a
        copy break the invariants the model enforces at construction.
        """
        if not update:
            return super().model_copy(deep=deep)
        return type(self).model_validate({**dict(self), **update})


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
    author_subject: str | None = None
    author_name: str | None = None

    @model_serializer(mode="wrap")
    def serialise(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        return {key: value for key, value in handler(self).items() if value is not None}


Citation = Annotated[ArtifactCitation | InterviewTurnCitation, Field(discriminator="kind")]


class Claim(_Model):
    text: NonBlank
    citations: tuple[Citation, ...] = ()


class Section(_Model):
    claims: tuple[Claim, ...] = ()
    """In order. For ``steps`` the order is the procedure."""
    open_question: NonBlank | None = None
    """What the expert could not answer. Set instead of guessing."""


@dataclass(frozen=True)
class UncitedClaim:
    """Where an uncited claim sits in an entry, and what it says."""

    section: SectionName
    index: int
    text: str

    def __str__(self) -> str:
        return f'uncited claim in {self.section}[{self.index}]: "{self.text}"'


class UncitedClaimError(ValueError):
    """Raised when an entry holds claims without a citation. Names every one of them."""

    def __init__(self, claims: tuple[UncitedClaim, ...]) -> None:
        self.claims = claims
        super().__init__("; ".join(str(claim) for claim in claims))


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
            # F9: uncited claims block publication, whichever code path built the entry.
            validate_citations(self)
        elif self.approved_by is not None or self.approved_at is not None:
            raise ValueError(
                f"a {self.status.value} entry must not carry approved_by or approved_at"
            )
        return self


def find_uncited_claims(entry: RunbookEntry) -> tuple[UncitedClaim, ...]:
    """Every claim without a citation, in template order then position.

    All seven sections count: anything written in the template is something a
    reader may act on, so there is no claim that is exempt from citation.
    """
    return tuple(
        UncitedClaim(section=name, index=index, text=claim.text)
        for name in SECTION_NAMES
        for index, claim in enumerate(entry.section(name).claims)
        if not claim.citations
    )


def validate_citations(entry: RunbookEntry) -> None:
    """Raise ``UncitedClaimError`` naming each uncited claim; return ``None`` if there is none."""
    uncited = find_uncited_claims(entry)
    if uncited:
        raise UncitedClaimError(uncited)
