"""Deliverable c (F1): the git MCP server is read-only and scoped to one expert."""

import re
import sys
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from mcp import Client, StdioServerParameters

from mcp_servers.common.source import SourceAccessError, SourceReader
from mcp_servers.git_server import build_server
from tests.support import (
    CORPUS,
    EXPERT_EMAIL,
    EXPERT_NAME,
    FAKE_CREDENTIALS,
    FULL_SPAN,
    REPO_ROOT,
    make_writable,
    raw_commits,
    raw_pull_requests,
    read_only_copy,
    tree_hashes,
)

pytestmark = pytest.mark.anyio

TOOLS = {
    "list_commits",
    "list_pull_requests",
    "list_documents",
    "get_document",
    "get_repo_activity",
    "get_path_activity",
}


@pytest.fixture
async def client() -> Any:
    async with Client(build_server(FULL_SPAN, CORPUS), raise_exceptions=True) as connected:
        yield connected


async def call(client: Client, name: str, arguments: dict[str, Any] | None = None) -> Any:
    result = await client.call_tool(name, arguments or {})
    assert not result.is_error, result.content
    return result.structured_content


async def all_commits(client: Client, **arguments: Any) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    offset: int | None = 0
    while offset is not None:
        page = await call(client, "list_commits", {"offset": offset, "limit": 50, **arguments})
        items.extend(page["items"])
        offset = page["next_offset"]
    return items


async def test_tools_are_read_only(client: Client) -> None:
    tools = (await client.list_tools()).tools
    assert {tool.name for tool in tools} == TOOLS
    for tool in tools:
        assert re.match(r"^(list|get)_", tool.name)
        assert tool.annotations is not None
        assert tool.annotations.read_only_hint is True


async def test_source_is_never_written(tmp_path: Path) -> None:
    corpus = read_only_copy(tmp_path)
    before = tree_hashes(corpus)
    try:
        async with Client(build_server(FULL_SPAN, corpus), raise_exceptions=True) as connected:
            await all_commits(connected)
            await call(connected, "list_pull_requests")
            await call(connected, "list_documents")
            await call(connected, "get_repo_activity")
            await call(
                connected, "get_path_activity", {"repo": "billing-batch", "paths": ["VERSION"]}
            )
        assert tree_hashes(corpus) == before
    finally:
        make_writable(corpus)


async def test_commits_are_scoped_to_the_expert(client: Client) -> None:
    returned = await all_commits(client)
    expected = [c for c in raw_commits() if c["author_email"] == EXPERT_EMAIL]
    assert {c["author_email"] for c in returned} == {EXPERT_EMAIL}
    assert {c["sha"] for c in returned} == {c["sha"] for c in expected}
    assert len(returned) == len(expected) > 100


async def test_repo_filter(client: Client) -> None:
    returned = await all_commits(client, repo="report-export")
    assert len(returned) == 45
    assert {c["repo"] for c in returned} == {"report-export"}


async def test_range_is_enforced() -> None:
    narrow = replace(
        FULL_SPAN,
        since=datetime(2025, 3, 1, tzinfo=UTC),
        until=datetime(2025, 5, 31, 23, 59, 59, tzinfo=UTC),
    )
    async with Client(build_server(narrow, CORPUS), raise_exceptions=True) as connected:
        returned = await all_commits(connected)
    expected = [
        c
        for c in raw_commits()
        if c["author_email"] == EXPERT_EMAIL
        and "2025-03-01" <= c["authored_at"][:10] <= "2025-05-31"
    ]
    assert expected, "the fixture should have commits in this window"
    assert {c["sha"] for c in returned} == {c["sha"] for c in expected}
    assert all("2025-03-01" <= c["authored_at"][:10] <= "2025-05-31" for c in returned)


async def test_paging_is_consistent(client: Client) -> None:
    first = await call(client, "list_commits", {"offset": 0, "limit": 10})
    assert len(first["items"]) == 10
    assert first["next_offset"] == 10
    assert first["total"] == len(await all_commits(client))
    second = await call(client, "list_commits", {"offset": 10, "limit": 10})
    assert not {c["sha"] for c in first["items"]} & {c["sha"] for c in second["items"]}


async def test_bad_paging_arguments_are_refused(client: Client) -> None:
    for arguments in ({"offset": -1}, {"limit": 0}, {"limit": 100000}):
        result = await client.call_tool("list_commits", arguments)
        assert result.is_error


async def test_pull_requests_include_reviewed_ones(client: Client) -> None:
    page = await call(client, "list_pull_requests", {"limit": 200})
    by_id = {p["id"]: p for p in page["items"]}
    rejected = [p for p in raw_pull_requests() if p["state"] == "rejected"]
    assert len(rejected) == 1
    assert rejected[0]["author"] != EXPERT_NAME
    returned = by_id[rejected[0]["id"]]
    assert returned["expert_role"] == "reviewer"
    assert any(
        r["reviewer"] == EXPERT_NAME and r["state"] == "rejected" for r in returned["reviews"]
    )
    for pull_request in page["items"]:
        involved = pull_request["author"] == EXPERT_NAME or any(
            r["reviewer"] == EXPERT_NAME for r in pull_request["reviews"]
        )
        assert involved


async def test_nothing_returned_contains_a_secret(client: Client) -> None:
    commits = await all_commits(client)
    pull_requests = await call(client, "list_pull_requests", {"limit": 200})
    documents = await call(client, "list_documents")
    everything = repr([commits, pull_requests, documents])
    for secret in FAKE_CREDENTIALS:
        assert secret not in everything
    holders = [c for c in commits if "[REDACTED:credential]" in (c["patch"] or "")]
    assert len(holders) == 1
    assert holders[0]["redaction_counts"]["credential"] == 1


async def test_repo_activity_has_counts_only(client: Client) -> None:
    activity = {a["repo"]: a for a in (await call(client, "get_repo_activity"))["result"]}
    assert activity["report-export"]["total_commits"] == 45
    assert activity["report-export"]["expert_commits"] == 45
    assert activity["report-export"]["other_authors"] == 0
    notify = [c for c in raw_commits() if c["repo"] == "notify-relay"]
    colleagues = {c["author_email"] for c in notify} - {EXPERT_EMAIL}
    assert activity["notify-relay"]["total_commits"] == len(notify) == 13
    assert activity["notify-relay"]["expert_commits"] == 1
    assert activity["notify-relay"]["other_authors"] == len(colleagues)
    for entry in activity.values():
        assert set(entry) == {
            "repo",
            "total_commits",
            "expert_commits",
            "other_authors",
            "first_commit_at",
            "last_commit_at",
        }


async def test_path_activity(client: Client) -> None:
    flags = await call(
        client,
        "get_path_activity",
        {
            "repo": "billing-batch",
            "paths": ["config/flags.yaml", "deploy/cron.yaml", "missing.txt"],
        },
    )
    by_path = {entry["path"]: entry for entry in flags["result"]}
    assert by_path["config/flags.yaml"]["total_commits"] == 16
    assert by_path["config/flags.yaml"]["expert_commits"] == 16
    assert by_path["config/flags.yaml"]["other_authors"] == 0
    assert by_path["deploy/cron.yaml"]["last_changed_at"].startswith("2025-12-09")
    assert by_path["missing.txt"]["total_commits"] == 0
    assert by_path["missing.txt"]["last_changed_at"] is None


async def test_documents_are_served(client: Client) -> None:
    documents = (await call(client, "list_documents"))["result"]
    assert len(documents) == 6
    page = await call(client, "get_document", {"document_id": "WIKI-ledger-db-failover"})
    assert page["title"] == "ledger-db failover"
    missing = await client.call_tool("get_document", {"document_id": "WIKI-nope"})
    assert missing.is_error


async def test_mail_and_answer_key_are_never_opened(tmp_path: Path) -> None:
    reader = SourceReader(CORPUS, allowed=("git", "wiki"))
    for forbidden in (
        "mail/Inbox/EML-0001.json",
        "../answer_key.json",
        "../expert_answers.json",
        "tickets/TCK-0001.json",
    ):
        with pytest.raises(SourceAccessError):
            reader.read_text(forbidden)
    corpus = read_only_copy(tmp_path, without=("mail", "tickets"))
    try:
        async with Client(build_server(FULL_SPAN, corpus), raise_exceptions=True) as connected:
            without_mail = await all_commits(connected)
        async with Client(build_server(FULL_SPAN, CORPUS), raise_exceptions=True) as connected:
            with_mail = await all_commits(connected)
        assert without_mail == with_mail
    finally:
        make_writable(corpus)


async def test_runs_over_stdio() -> None:
    server = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "mcp_servers.git_server",
            "--source-dir",
            str(CORPUS),
            "--expert-name",
            EXPERT_NAME,
            "--expert-email",
            EXPERT_EMAIL,
            "--since",
            "2024-11-01",
            "--until",
            "2026-10-31",
            "--work-domain",
            "example.com",
        ],
        cwd=str(REPO_ROOT),
    )
    async with Client(server) as connected:
        assert {tool.name for tool in (await connected.list_tools()).tools} == TOOLS
        page = await call(connected, "list_commits", {"limit": 5})
        assert len(page["items"]) == 5
