"""Small builders for hand-made snapshots, so mapping tests need no fixture corpus."""

from datetime import UTC, datetime
from typing import Any

from eki.ingest.models import ActivitySummary, Artifact, Snapshot
from eki.ingest.redaction import RedactedText

EXPERT = "Nadia Ferrand"
AS_OF = datetime(2026, 10, 31, 23, 59, 59, tzinfo=UTC)


def when(year: int, month: int, day: int = 15) -> datetime:
    return datetime(year, month, day, 12, 0, tzinfo=UTC)


def commit(
    sha: str,
    repo: str,
    message: str,
    paths: tuple[str, ...] = ("src/app.py",),
    *,
    at: datetime | None = None,
) -> Artifact:
    return Artifact(
        id=sha,
        source="git",
        kind="commit",
        system=repo,
        occurred_at=at or when(2026, 6),
        title=RedactedText(message),
        body=RedactedText(message),
        expert_role="author",
        metadata={
            "repo": repo,
            "pr_id": None,
            "files": [{"path": p, "additions": 1, "deletions": 0} for p in paths],
        },
        redaction_counts={},
    )


def ticket(
    ticket_id: str,
    system: str,
    title: str,
    *,
    kind: str = "incident",
    priority: str = "P3",
    role: str = "assignee",
    at: datetime | None = None,
) -> Artifact:
    return Artifact(
        id=ticket_id,
        source="tickets",
        kind="ticket",
        system=system,
        occurred_at=at or when(2026, 6),
        title=RedactedText(title),
        body=RedactedText(title),
        expert_role=role,
        metadata={
            "type": kind,
            "priority": priority,
            "status": "closed",
            "assignee": EXPERT,
            "reporter": "Someone",
            "resolved_at": None,
            "labels": [],
            "comments": [],
        },
        redaction_counts={},
    )


def pull_request(
    pr_id: str,
    repo: str,
    title: str,
    review_state: str,
    *,
    at: datetime | None = None,
) -> Artifact:
    return Artifact(
        id=pr_id,
        source="git",
        kind="pull_request",
        system=repo,
        occurred_at=at or when(2026, 6),
        title=RedactedText(title),
        body=RedactedText(title),
        expert_role="reviewer",
        metadata={
            "repo": repo,
            "author": "Someone",
            "state": "closed",
            "closed_at": None,
            "commit_shas": [],
            "reviews": [{"reviewer": EXPERT, "state": review_state, "at": "x", "body": "ok"}],
        },
        redaction_counts={},
    )


def document(slug: str, title: str, body: str, last_updated: str = "2025-01-01") -> Artifact:
    year, month, day = (int(part) for part in last_updated.split("-"))
    return Artifact(
        id=f"WIKI-{slug}",
        source="git",
        kind="document",
        system=None,
        occurred_at=datetime(year, month, day, tzinfo=UTC),
        title=RedactedText(title),
        body=RedactedText(body),
        expert_role="none",
        metadata={"slug": slug, "last_updated": last_updated, "authors": []},
        redaction_counts={},
    )


def activity(
    scope: str,
    system: str,
    key: str,
    total: int,
    by_expert: int,
    other_actors: int = 0,
    last_at: datetime | None = None,
) -> ActivitySummary:
    return ActivitySummary.model_validate(
        {
            "scope": scope,
            "system": system,
            "key": key,
            "total": total,
            "by_expert": by_expert,
            "other_actors": other_actors,
            "last_at": last_at,
        }
    )


def snapshot(
    artifacts: list[Artifact], activities: list[ActivitySummary] | None = None
) -> Snapshot:
    fields: dict[str, Any] = {
        "expert_name": EXPERT,
        "expert_email": "nadia.ferrand@example.com",
        "since": when(2024, 11, 1),
        "until": AS_OF,
        "artifacts": tuple(artifacts),
        "activity": tuple(activities or []),
    }
    return Snapshot.model_validate(fields)
