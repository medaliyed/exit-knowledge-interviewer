"""Deliverable f (F1, F2): ingest pulls through MCP and keeps only redacted data."""

import logging
from typing import Any

import pytest
from mcp import Client
from mcp.server import MCPServer

from eki.ingest.pipeline import IngestError, ingest_errors, ingest_from_source, pull
from mcp_servers.git_server import build_server as build_git
from mcp_servers.tickets_server import build_server as build_tickets
from tests.support import (
    CORPUS,
    EXPERT_EMAIL,
    FAKE_CREDENTIALS,
    FULL_SPAN,
    raw_commits,
    raw_pull_requests,
    raw_tickets,
)

pytestmark = pytest.mark.anyio

LEAK = "password=FAKE-PASSWORD-leaked-Zz9"


async def real_pull() -> Any:
    async with (
        Client(build_git(FULL_SPAN, CORPUS), raise_exceptions=True) as git,
        Client(build_tickets(FULL_SPAN, CORPUS), raise_exceptions=True) as tickets,
    ):
        return await pull(git, tickets, FULL_SPAN)


def fake_servers(
    *, commit_message: str = "fix: tidy", drop_sha: bool = False
) -> tuple[MCPServer, MCPServer]:
    """Servers that behave badly: they return unredacted text, or malformed items."""
    commit: dict[str, Any] = {
        "sha": "a" * 40,
        "repo": "billing-batch",
        "authored_at": "2025-01-02T10:00:00Z",
        "author_name": "Nadia Ferrand",
        "author_email": EXPERT_EMAIL,
        "message": commit_message,
        "files": [{"path": "posting/job.py", "additions": 1, "deletions": 0}],
        "patch": None,
        "pr_id": None,
        "redaction_counts": {},
    }
    if drop_sha:
        del commit["sha"]
    git = MCPServer("fake-git")
    tickets = MCPServer("fake-tickets")

    @git.tool()
    def list_commits(offset: int = 0, limit: int = 100) -> dict[str, Any]:
        return {"items": [commit], "total": 1, "next_offset": None}

    @git.tool()
    def list_pull_requests(offset: int = 0, limit: int = 100) -> dict[str, Any]:
        return {"items": [], "total": 0, "next_offset": None}

    @git.tool()
    def list_documents() -> list[dict[str, Any]]:
        return []

    @git.tool()
    def get_repo_activity() -> list[dict[str, Any]]:
        return []

    @git.tool()
    def get_path_activity(repo: str, paths: list[str]) -> list[dict[str, Any]]:
        return []

    @tickets.tool()
    def list_tickets(offset: int = 0, limit: int = 100) -> dict[str, Any]:
        return {"items": [], "total": 0, "next_offset": None}

    @tickets.tool()
    def get_ticket_activity() -> list[dict[str, Any]]:
        return []

    return git, tickets


async def pull_from(git_server: MCPServer, tickets_server: MCPServer) -> Any:
    with ingest_errors():
        async with Client(git_server) as git, Client(tickets_server) as tickets:
            return await pull(git, tickets, FULL_SPAN)


async def test_pull_collects_all_in_scope_artifacts() -> None:
    snapshot = await real_pull()
    kinds = [a.kind for a in snapshot.artifacts]
    expert_commits = [c for c in raw_commits() if c["author_email"] == EXPERT_EMAIL]
    assert kinds.count("commit") == len(expert_commits)
    assert kinds.count("document") == 6
    assert kinds.count("pull_request") == len(
        [
            p
            for p in raw_pull_requests()
            if p["author"] == "Nadia Ferrand"
            or any(r["reviewer"] == "Nadia Ferrand" for r in p["reviews"])
        ]
    )
    involved = [
        t
        for t in raw_tickets()
        if "Nadia Ferrand" in (t["assignee"], t["reporter"], *(c["author"] for c in t["comments"]))
    ]
    assert kinds.count("ticket") == len(involved)
    assert len({a.id for a in snapshot.artifacts}) == len(snapshot.artifacts)
    assert snapshot.expert_email == EXPERT_EMAIL


async def test_artifacts_and_activity_are_sorted_and_stable() -> None:
    first, second = await real_pull(), await real_pull()
    order = [(a.occurred_at, a.id) for a in first.artifacts]
    assert order == sorted(order)
    assert first == second


async def test_documents_have_no_expert_role() -> None:
    snapshot = await real_pull()
    documents = [a for a in snapshot.artifacts if a.kind == "document"]
    assert {a.expert_role for a in documents} == {"none"}
    assert all(a.id.startswith("WIKI-") for a in documents)


async def test_activity_covers_repos_tickets_and_document_paths() -> None:
    snapshot = await real_pull()
    by_scope: dict[str, list[Any]] = {"repo": [], "path": [], "ticket_group": []}
    for entry in snapshot.activity:
        by_scope[entry.scope].append(entry)
    assert {e.key for e in by_scope["repo"]} >= {"report-export", "billing-batch", "notify-relay"}
    cron = [
        e for e in by_scope["path"] if e.system == "billing-batch" and e.key == "deploy/cron.yaml"
    ]
    assert cron
    assert cron[0].last_at is not None
    assert cron[0].last_at.year == 2025
    assert by_scope["ticket_group"]


async def test_every_text_field_is_redacted_even_if_a_server_leaks() -> None:
    git, tickets = fake_servers(commit_message=f"fix: tidy {LEAK}")
    snapshot = await pull_from(git, tickets)
    (commit,) = snapshot.artifacts
    assert "FAKE-PASSWORD-leaked-Zz9" not in commit.title
    assert "FAKE-PASSWORD-leaked-Zz9" not in commit.body
    assert "[REDACTED:credential]" in commit.body
    assert commit.redaction_counts["credential"] >= 1


async def test_real_pull_contains_no_secret() -> None:
    snapshot = await real_pull()
    everything = repr(snapshot)
    for secret in FAKE_CREDENTIALS:
        assert secret not in everything


async def test_logs_carry_no_content(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    snapshot = await real_pull()
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "eki.ingest" in {record.name for record in caplog.records}
    for artifact in snapshot.artifacts[:200]:
        assert artifact.title not in logged
        assert artifact.body[:60] not in logged
    for secret in FAKE_CREDENTIALS:
        assert secret not in logged


async def test_malformed_server_output_is_rejected() -> None:
    git, tickets = fake_servers(drop_sha=True)
    with pytest.raises(IngestError, match="list_commits"):
        await pull_from(git, tickets)


async def test_a_failing_tool_is_reported() -> None:
    _, tickets = fake_servers()
    broken = MCPServer("broken-git")

    @broken.tool()
    def list_commits(offset: int = 0, limit: int = 100) -> dict[str, Any]:
        raise RuntimeError("boom")

    with pytest.raises(IngestError, match="list_commits"):
        await pull_from(broken, tickets)


async def test_pull_over_stdio_matches_in_memory() -> None:
    in_memory = await real_pull()
    over_stdio = await ingest_from_source(FULL_SPAN, CORPUS)
    assert over_stdio == in_memory
