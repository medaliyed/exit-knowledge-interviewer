"""Read-only MCP server over git history and wiki pages, for one expert and a date range.

Run: ``python -m mcp_servers.git_server --source-dir DIR --expert-name N --expert-email E
--since YYYY-MM-DD --until YYYY-MM-DD --work-domain example.com``

Every tool reads. None writes. All returned text is redacted here, so unredacted
content never crosses the protocol. Content tools return only what the expert
authored or reviewed; the activity tools return counts and dates, never text.
"""

from collections import defaultdict
from pathlib import Path
from typing import Any

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from mcp_servers.common.cli import parse_arguments
from mcp_servers.common.models import (
    CommitItem,
    CommitPage,
    DocumentItem,
    FileChange,
    PathActivity,
    PullRequestItem,
    PullRequestPage,
    RepoActivity,
    ReviewItem,
)
from mcp_servers.common.source import Redactor, Scope, SourceReader, parse_time

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)
MAX_LIMIT = 200


def _page_bounds(offset: int, limit: int) -> None:
    if offset < 0:
        raise ToolError("offset must be 0 or more")
    if not 1 <= limit <= MAX_LIMIT:
        raise ToolError(f"limit must be between 1 and {MAX_LIMIT}")


def _next_offset(offset: int, count: int, total: int) -> int | None:
    return offset + count if offset + count < total else None


def _parse_front_matter(text: str) -> tuple[dict[str, str], str]:
    _, front_matter, body = text.split("---\n", 2)
    meta = dict(line.split(": ", 1) for line in front_matter.splitlines() if line)
    return meta, body


def build_server(scope: Scope, source_dir: Path) -> MCPServer:
    reader = SourceReader(source_dir, allowed=("git", "wiki"))
    all_commits = [
        c
        for c in reader.read_jsonl("git/commits.jsonl")
        if scope.contains(parse_time(c["authored_at"]))
    ]
    pull_requests = reader.read_jsonl("git/pull_requests.jsonl")
    pages = []
    for name in reader.list_files("wiki", "*.md"):
        meta, body = _parse_front_matter(reader.read_text(name))
        pages.append((Path(name).stem, meta, body))

    server = MCPServer("eki-git")

    def commit_item(raw: dict[str, Any]) -> CommitItem:
        redact = Redactor(scope.work_email_domains)
        return CommitItem(
            sha=raw["sha"],
            repo=raw["repo"],
            authored_at=raw["authored_at"],
            author_name=raw["author_name"],
            author_email=redact(raw["author_email"]),
            message=redact(raw["message"]),
            files=[
                FileChange(
                    path=redact(f["path"]), additions=f["additions"], deletions=f["deletions"]
                )
                for f in raw["files"]
            ],
            patch=redact.optional(raw["patch"]),
            pr_id=raw["pr_id"],
            redaction_counts=redact.counts,
        )

    @server.tool(annotations=READ_ONLY)
    def list_commits(repo: str | None = None, offset: int = 0, limit: int = 100) -> CommitPage:
        """Commits the expert authored in the date range, oldest first, optionally in one repo."""
        _page_bounds(offset, limit)
        mine = [
            c
            for c in all_commits
            if c["author_email"] == scope.expert_email and (repo is None or c["repo"] == repo)
        ]
        window = mine[offset : offset + limit]
        return CommitPage(
            items=[commit_item(c) for c in window],
            total=len(mine),
            next_offset=_next_offset(offset, len(window), len(mine)),
        )

    def pull_request_item(raw: dict[str, Any], role: str) -> PullRequestItem:
        redact = Redactor(scope.work_email_domains)
        return PullRequestItem(
            id=raw["id"],
            repo=raw["repo"],
            title=redact(raw["title"]),
            author=raw["author"],
            state=raw["state"],
            created_at=raw["created_at"],
            closed_at=raw["closed_at"],
            description=redact(raw["description"]),
            commit_shas=raw["commit_shas"],
            reviews=[
                ReviewItem(
                    reviewer=r["reviewer"], state=r["state"], at=r["at"], body=redact(r["body"])
                )
                for r in raw["reviews"]
            ],
            expert_role=role,
            redaction_counts=redact.counts,
        )

    @server.tool(annotations=READ_ONLY)
    def list_pull_requests(offset: int = 0, limit: int = 100) -> PullRequestPage:
        """Pull requests the expert opened or reviewed, created in the date range."""
        _page_bounds(offset, limit)
        involved: list[tuple[dict[str, Any], str]] = []
        for raw in pull_requests:
            if not scope.contains(parse_time(raw["created_at"])):
                continue
            if raw["author"] == scope.expert_name:
                involved.append((raw, "author"))
            elif any(r["reviewer"] == scope.expert_name for r in raw["reviews"]):
                involved.append((raw, "reviewer"))
        window = involved[offset : offset + limit]
        return PullRequestPage(
            items=[pull_request_item(raw, role) for raw, role in window],
            total=len(involved),
            next_offset=_next_offset(offset, len(window), len(involved)),
        )

    def document_item(slug: str, meta: dict[str, str], body: str) -> DocumentItem:
        redact = Redactor(scope.work_email_domains)
        return DocumentItem(
            id=meta["id"],
            slug=slug,
            title=redact(meta["title"]),
            last_updated=meta["last_updated"],
            authors=[name.strip() for name in meta["authors"].split(",")],
            body=redact(body),
            redaction_counts=redact.counts,
        )

    @server.tool(annotations=READ_ONLY)
    def list_documents() -> list[DocumentItem]:
        """All wiki pages."""
        return [document_item(*page) for page in pages]

    @server.tool(annotations=READ_ONLY)
    def get_document(document_id: str) -> DocumentItem:
        """One wiki page by its ID, for example WIKI-ledger-db-failover."""
        for slug, meta, body in pages:
            if meta["id"] == document_id:
                return document_item(slug, meta, body)
        raise ToolError(f"unknown document: {document_id}")

    def activity_of(commits: list[dict[str, Any]]) -> tuple[int, int, int]:
        others = {c["author_email"] for c in commits} - {scope.expert_email}
        mine = sum(c["author_email"] == scope.expert_email for c in commits)
        return len(commits), mine, len(others)

    @server.tool(annotations=READ_ONLY)
    def get_repo_activity() -> list[RepoActivity]:
        """Per repo in the date range: commit counts and dates. No messages, no paths."""
        by_repo: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for commit in all_commits:
            by_repo[commit["repo"]].append(commit)
        results = []
        for repo in sorted(by_repo):
            commits = by_repo[repo]
            total, mine, others = activity_of(commits)
            times = sorted(c["authored_at"] for c in commits)
            results.append(
                RepoActivity(
                    repo=repo,
                    total_commits=total,
                    expert_commits=mine,
                    other_authors=others,
                    first_commit_at=times[0],
                    last_commit_at=times[-1],
                )
            )
        return results

    @server.tool(annotations=READ_ONLY)
    def get_path_activity(repo: str, paths: list[str]) -> list[PathActivity]:
        """Per file path in one repo: commit counts and last change. No messages."""
        results = []
        for path in paths:
            touching = [
                c
                for c in all_commits
                if c["repo"] == repo and any(f["path"] == path for f in c["files"])
            ]
            total, mine, others = activity_of(touching)
            results.append(
                PathActivity(
                    repo=repo,
                    path=path,
                    total_commits=total,
                    expert_commits=mine,
                    other_authors=others,
                    last_changed_at=max((c["authored_at"] for c in touching), default=None),
                )
            )
        return results

    return server


def main(argv: list[str] | None = None) -> None:
    scope, source_dir = parse_arguments("Read-only git MCP server", argv)
    build_server(scope, source_dir).run("stdio")


if __name__ == "__main__":
    main()
