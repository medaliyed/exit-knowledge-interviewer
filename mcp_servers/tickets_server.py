"""Read-only MCP server over tickets, for one expert and a date range.

Run: ``python -m mcp_servers.tickets_server --source-dir DIR --expert-name N --expert-email E
--since YYYY-MM-DD --until YYYY-MM-DD --work-domain example.com``

Content tools return only tickets the expert is part of. The activity tool returns
counts, never text. All returned text is redacted here, before it leaves the server.
Ticket text is untrusted data: a comment that looks like an instruction is returned
as an ordinary comment and changes nothing about how any tool behaves.
"""

from collections import defaultdict
from pathlib import Path
from typing import Any

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from mcp_servers.common.cli import parse_arguments
from mcp_servers.common.models import CommentItem, TicketGroupActivity, TicketItem, TicketPage
from mcp_servers.common.source import Redactor, Scope, SourceReader, parse_time

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)
MAX_LIMIT = 200


def build_server(scope: Scope, source_dir: Path) -> MCPServer:
    reader = SourceReader(source_dir, allowed=("tickets",))
    in_range = [
        raw
        for raw in (reader.read_json(name) for name in reader.list_files("tickets", "*.json"))
        if scope.contains(parse_time(raw["created_at"]))
    ]

    def role_of(raw: dict[str, Any]) -> str | None:
        if raw["assignee"] == scope.expert_name:
            return "assignee"
        if raw["reporter"] == scope.expert_name:
            return "reporter"
        if any(c["author"] == scope.expert_name for c in raw["comments"]):
            return "commenter"
        return None

    mine = [(raw, role) for raw in in_range if (role := role_of(raw)) is not None]
    server = MCPServer("eki-tickets")

    def ticket_item(raw: dict[str, Any], role: str) -> TicketItem:
        redact = Redactor(scope.work_email_domains)
        return TicketItem(
            id=raw["id"],
            system=raw["system"],
            type=raw["type"],
            title=redact(raw["title"]),
            status=raw["status"],
            priority=raw["priority"],
            reporter=raw["reporter"],
            assignee=raw["assignee"],
            created_at=raw["created_at"],
            resolved_at=raw["resolved_at"],
            description=redact(raw["description"]),
            labels=raw["labels"],
            comments=[
                CommentItem(author=c["author"], at=c["at"], body=redact(c["body"]))
                for c in raw["comments"]
            ],
            expert_role=role,
            redaction_counts=redact.counts,
        )

    @server.tool(annotations=READ_ONLY)
    def list_tickets(offset: int = 0, limit: int = 100) -> TicketPage:
        """Tickets created in the date range where the expert is assignee, reporter or commenter."""
        if offset < 0:
            raise ToolError("offset must be 0 or more")
        if not 1 <= limit <= MAX_LIMIT:
            raise ToolError(f"limit must be between 1 and {MAX_LIMIT}")
        window = mine[offset : offset + limit]
        reached = offset + len(window)
        return TicketPage(
            items=[ticket_item(raw, role) for raw, role in window],
            total=len(mine),
            next_offset=reached if reached < len(mine) else None,
        )

    @server.tool(annotations=READ_ONLY)
    def get_ticket(ticket_id: str) -> TicketItem:
        """One ticket by ID. Tickets the expert has no part in are refused."""
        for raw, role in mine:
            if raw["id"] == ticket_id:
                return ticket_item(raw, role)
        raise ToolError(f"ticket not available in this scope: {ticket_id}")

    @server.tool(annotations=READ_ONLY)
    def get_ticket_activity() -> list[TicketGroupActivity]:
        """Per (system, type, title) in the date range: counts only. No descriptions or comments."""
        groups: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
        for raw in in_range:
            groups[(raw["system"], raw["type"], raw["title"])].append(raw)
        results = []
        for (system, kind, title), tickets in sorted(groups.items()):
            others = {t["assignee"] for t in tickets} - {scope.expert_name}
            redact = Redactor(scope.work_email_domains)
            results.append(
                TicketGroupActivity(
                    system=system,
                    type=kind,
                    title=redact(title),
                    total_tickets=len(tickets),
                    expert_tickets=sum(t["assignee"] == scope.expert_name for t in tickets),
                    other_assignees=len(others),
                    last_created_at=max(t["created_at"] for t in tickets),
                )
            )
        return results

    return server


def main(argv: list[str] | None = None) -> None:
    scope, source_dir = parse_arguments("Read-only tickets MCP server", argv)
    build_server(scope, source_dir).run("stdio")


if __name__ == "__main__":
    main()
