"""What the MCP servers return. Every text field has already been redacted."""

from pydantic import BaseModel


class FileChange(BaseModel):
    path: str
    additions: int
    deletions: int


class CommitItem(BaseModel):
    sha: str
    repo: str
    authored_at: str
    author_name: str
    author_email: str
    message: str
    files: list[FileChange]
    patch: str | None
    pr_id: str | None
    redaction_counts: dict[str, int]


class CommitPage(BaseModel):
    items: list[CommitItem]
    total: int
    next_offset: int | None


class ReviewItem(BaseModel):
    reviewer: str
    state: str
    at: str
    body: str


class PullRequestItem(BaseModel):
    id: str
    repo: str
    title: str
    author: str
    state: str
    created_at: str
    closed_at: str | None
    description: str
    commit_shas: list[str]
    reviews: list[ReviewItem]
    expert_role: str
    """``author`` if the expert opened it, otherwise ``reviewer``."""
    redaction_counts: dict[str, int]


class PullRequestPage(BaseModel):
    items: list[PullRequestItem]
    total: int
    next_offset: int | None


class DocumentItem(BaseModel):
    id: str
    slug: str
    title: str
    last_updated: str
    authors: list[str]
    body: str
    redaction_counts: dict[str, int]


class RepoActivity(BaseModel):
    """Counts only. Never a message or a path."""

    repo: str
    total_commits: int
    expert_commits: int
    other_authors: int
    first_commit_at: str | None
    last_commit_at: str | None


class PathActivity(BaseModel):
    repo: str
    path: str
    total_commits: int
    expert_commits: int
    other_authors: int
    last_changed_at: str | None


class CommentItem(BaseModel):
    author: str
    at: str
    body: str


class TicketItem(BaseModel):
    id: str
    system: str
    type: str
    title: str
    status: str
    priority: str
    reporter: str
    assignee: str
    created_at: str
    resolved_at: str | None
    description: str
    labels: list[str]
    comments: list[CommentItem]
    expert_role: str
    """``assignee``, ``reporter`` or ``commenter``."""
    redaction_counts: dict[str, int]


class TicketPage(BaseModel):
    items: list[TicketItem]
    total: int
    next_offset: int | None


class TicketGroupActivity(BaseModel):
    """Counts only. Never a description or a comment."""

    system: str
    type: str
    title: str
    total_tickets: int
    expert_tickets: int
    other_assignees: int
    last_created_at: str | None
