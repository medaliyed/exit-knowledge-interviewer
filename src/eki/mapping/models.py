"""Topics and gap scores."""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class TopicKind(StrEnum):
    SYSTEM = "system"
    PROCEDURE = "procedure"
    INCIDENT = "incident"
    DECISION = "decision"
    CONTACT = "contact"
    """Not produced until mail arrives in phase 4, where the vendor contact lives."""


class Topic(_Model):
    id: str
    """Stable: kind, system and a slug of the label."""
    kind: TopicKind
    system: str | None
    label: str
    artifact_ids: tuple[str, ...]


class DocStatus(StrEnum):
    NONE = "none"
    PARTIAL = "partial"
    DOCUMENTED = "documented"


class GapScore(_Model):
    topic_id: str
    exclusivity: float
    doc_gap: float
    recency: float
    criticality: float
    """Each of the four components is between 0 and 1."""
    score: float
    """0 to 100."""
    doc_status: DocStatus
    linked_documents: tuple[str, ...]
    explanation: dict[str, str]
