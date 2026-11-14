"""The shapes the MCP servers send. Validated strictly: server output is untrusted.

These are deliberately separate from the servers' own models. The client must not
import server code, and must reject anything that does not match what it expects.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _Wire(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class WireFile(_Wire):
    path: str
    additions: int
    deletions: int


class WireCommit(_Wire):
    sha: str
    repo: str
    authored_at: str
    author_name: str
    author_email: str
    message: str
    files: list[WireFile]
    patch: str | None
    pr_id: str | None


class WireCommitPage(_Wire):
    items: list[WireCommit]
    total: int
    next_offset: int | None


class WireReview(_Wire):
    reviewer: str
    state: str
    at: str
    body: str


class WirePullRequest(_Wire):
    id: str
    repo: str
    title: str
    author: str
    state: str
    created_at: str
    closed_at: str | None
    description: str
    commit_shas: list[str]
    reviews: list[WireReview]
    expert_role: str


class WirePullRequestPage(_Wire):
    items: list[WirePullRequest]
    total: int
    next_offset: int | None


class WireDocument(_Wire):
    id: str
    slug: str
    title: str
    last_updated: str
    authors: list[str]
    body: str


class WireRepoActivity(_Wire):
    repo: str
    total_commits: int
    expert_commits: int
    other_authors: int
    first_commit_at: str | None
    last_commit_at: str | None


class WirePathActivity(_Wire):
    repo: str
    path: str
    total_commits: int
    expert_commits: int
    other_authors: int
    last_changed_at: str | None


class WireComment(_Wire):
    author: str
    at: str
    body: str


class WireTicket(_Wire):
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
    comments: list[WireComment]
    expert_role: str


class WireTicketPage(_Wire):
    items: list[WireTicket]
    total: int
    next_offset: int | None


class WireTicketGroup(_Wire):
    system: str
    type: str
    title: str
    total_tickets: int
    expert_tickets: int
    other_assignees: int
    last_created_at: str | None


class WireMail(_Wire):
    id: str = Field(pattern=r"^EML-[0-9]{4,}$")
    thread_id: str = Field(pattern=r"^THR-[0-9]{4,}$")
    folder: str
    sensitivity: Literal["normal"]
    sender: str
    to: list[str]
    cc: list[str]
    sent_at: str
    subject: str
    body: str
    expert_role: Literal["author", "recipient"]


class WireMailPage(_Wire):
    items: list[WireMail]
    total: int
    next_offset: int | None
