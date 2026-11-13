"""What ingest produces and stores. Every text field is redacted text."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from eki.ingest.redaction import RedactedText


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Artifact(_Model):
    id: str
    source: Literal["git", "tickets"]
    kind: Literal["commit", "pull_request", "ticket", "document"]
    system: str | None
    occurred_at: datetime
    title: RedactedText
    body: RedactedText
    expert_role: str
    """author, assignee, reporter, commenter, reviewer, or none (documents)."""
    metadata: dict[str, Any]
    redaction_counts: dict[str, int]


class ActivitySummary(_Model):
    """Counts of work by the expert and by others, with no content."""

    scope: Literal["repo", "path", "ticket_group"]
    system: str
    key: str
    total: int
    by_expert: int
    other_actors: int
    last_at: datetime | None


class Snapshot(_Model):
    expert_name: str
    expert_email: str
    since: datetime
    until: datetime
    artifacts: tuple[Artifact, ...]
    activity: tuple[ActivitySummary, ...]
