"""Read-only mail over the synthetic JSON source, with immutable selected-folder scope."""

from pathlib import Path

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from eki.ingest.scope import validate_mail_folders
from mcp_servers.common.cli import parse_arguments
from mcp_servers.common.models import MailItem, MailPage
from mcp_servers.common.source import Redactor, Scope, SourceAccessError, SourceReader, parse_time


class _Message(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str = Field(pattern=r"^EML-[0-9]{4,}$")
    thread_id: str = Field(pattern=r"^THR-[0-9]{4,}$")
    folder: str
    sensitivity: str
    sender: str = Field(validation_alias="from")
    to: list[str]
    cc: list[str] = []
    sent_at: str
    subject: str
    body: str


def build_server(scope: Scope, source_dir: Path) -> MCPServer:
    folders = validate_mail_folders(scope.mail_folders, required=True)
    if scope.expert_email.rpartition("@")[2].casefold() not in scope.work_email_domains:
        raise ValueError("mail expert email must belong to a configured work domain")
    reader = SourceReader(source_dir, allowed=tuple("mail/" + f for f in folders))
    messages: list[MailItem] = []
    seen: set[str] = set()
    for folder in folders:
        directory = "mail/" + folder
        if not reader.resolve(directory).is_dir():
            raise SourceAccessError("selected mail folder does not exist")
        for filename in reader.list_files(directory, "*.json"):
            try:
                raw = _Message.model_validate(reader.read_json(filename))
                if raw.folder != folder:
                    raise SourceAccessError("mail message does not belong to selected folder")
                if raw.sensitivity != "normal":
                    continue
                if not scope.contains(parse_time(raw.sent_at)):
                    continue
                if scope.expert_email.casefold() not in {
                    a.strip().casefold() for a in (raw.sender, *raw.to, *raw.cc)
                }:
                    continue
                if raw.id in seen:
                    raise SourceAccessError("duplicate mail message identifier")
                seen.add(raw.id)
                clean = Redactor(scope.work_email_domains)
                messages.append(
                    MailItem(
                        id=raw.id,
                        thread_id=raw.thread_id,
                        folder=clean(folder),
                        sender=clean(raw.sender),
                        to=[clean(a) for a in raw.to],
                        cc=[clean(a) for a in raw.cc],
                        sent_at=raw.sent_at,
                        subject=clean(raw.subject),
                        body=clean(raw.body),
                        expert_role="author"
                        if raw.sender.strip().casefold() == scope.expert_email.casefold()
                        else "recipient",
                        redaction_counts=clean.counts,
                    )
                )
            except (ValidationError, ValueError, OSError):
                raise SourceAccessError(
                    "invalid or inaccessible message in selected mail folder"
                ) from None
    messages.sort(key=lambda m: (m.sent_at, m.id))
    by_id = {m.id: m for m in messages}
    server = MCPServer("eki-mail")
    readonly = ToolAnnotations(read_only_hint=True, open_world_hint=False)

    @server.tool(annotations=readonly)
    def list_messages(offset: int = 0, limit: int = 100) -> MailPage:
        """Normal, expert-involved messages in the fixed folders/date range, oldest first."""
        if offset < 0 or not 1 <= limit <= 200:
            raise ToolError("invalid mail pagination")
        window = messages[offset : offset + limit]
        following = offset + len(window)
        return MailPage(
            items=window,
            total=len(messages),
            next_offset=following if following < len(messages) else None,
        )

    @server.tool(annotations=readonly)
    def get_message(message_id: str) -> MailItem:
        """One already-scoped message by ID. Never opens a path supplied by a caller."""
        message = by_id.get(message_id)
        if message is None:
            raise ToolError("mail message not found")
        return message

    return server


def main(argv: list[str] | None = None) -> None:
    scope, source = parse_arguments("Read-only selected-folder mail MCP server", argv)
    build_server(scope, source).run("stdio")


if __name__ == "__main__":
    main()
