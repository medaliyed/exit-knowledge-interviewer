"""The scope of one ingest: one expert, one date range."""

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Scope:
    expert_name: str
    expert_email: str
    since: datetime
    until: datetime
    """Inclusive on both ends. Both are timezone-aware."""
    work_email_domains: frozenset[str]

    def contains(self, moment: datetime) -> bool:
        return self.since <= moment <= self.until
