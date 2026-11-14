"""F3: the client rechecks mail policy and the default pipeline still excludes mail."""

from dataclasses import replace
from typing import Any

import pytest
from mcp import Client
from mcp.server import MCPServer

from eki.ingest.pipeline import IngestError, ingest_errors, ingest_from_source, pull
from eki.ingest.wire import WireMail
from mcp_servers.git_server import build_server as build_git
from mcp_servers.mail_server import build_server as build_mail
from mcp_servers.tickets_server import build_server as build_tickets
from tests.support import CORPUS, FAKE_CREDENTIALS, FULL_SPAN

pytestmark = [pytest.mark.anyio, pytest.mark.redteam]


async def test_mail_is_off_by_default_and_selected_over_stdio() -> None:
    default = await ingest_from_source(FULL_SPAN, CORPUS)
    assert not [a for a in default.artifacts if a.source == "mail"]
    scope = replace(FULL_SPAN, mail_folders=("Inbox", "Sent", "Vendors"))
    over_stdio = await ingest_from_source(scope, CORPUS)
    with ingest_errors():
        async with (
            Client(build_git(scope, CORPUS)) as git,
            Client(build_tickets(scope, CORPUS)) as tickets,
            Client(build_mail(scope, CORPUS)) as mail,
        ):
            in_memory = await pull(git, tickets, scope, mail)
    assert over_stdio == in_memory
    assert over_stdio.mail_folders == ("Inbox", "Sent", "Vendors")
    messages = [a for a in over_stdio.artifacts if a.source == "mail"]
    assert messages and all(a.metadata["sensitivity"] == "normal" for a in messages)
    assert all(secret not in over_stdio.model_dump_json() for secret in FAKE_CREDENTIALS)


@pytest.mark.parametrize(
    "change",
    [
        {"folder": "Personal"},
        {"sensitivity": "private"},
        {"sender": "other@example.com", "to": [], "cc": []},
        {"sent_at": "2020-01-01T00:00:00Z"},
        {"sent_at": "invalid"},
        {"expert_role": "author"},
    ],
)
async def test_untrusted_mail_server_cannot_widen_scope(change: dict[str, Any]) -> None:
    scope = replace(FULL_SPAN, mail_folders=("Inbox",))
    message = {
        "id": "EML-9999",
        "thread_id": "THR-9999",
        "folder": "Inbox",
        "sensitivity": "normal",
        "sender": "other@example.com",
        "to": [scope.expert_email],
        "cc": [],
        "sent_at": "2025-01-02T10:00:00Z",
        "subject": "Untrusted source",
        "body": "Must not escape scope",
        "expert_role": "recipient",
        **change,
    }
    evil = MCPServer("evil-mail")

    @evil.tool()
    def list_messages(offset: int = 0, limit: int = 100) -> dict[str, Any]:
        return {"items": [message], "total": 1, "next_offset": None}

    with pytest.raises(IngestError), ingest_errors():
        async with (
            Client(build_git(scope, CORPUS)) as git,
            Client(build_tickets(scope, CORPUS)) as tickets,
            Client(evil) as mail,
        ):
            await pull(git, tickets, scope, mail)


def test_mail_wire_refuses_unknown_private_sensitivity() -> None:
    with pytest.raises(ValueError):
        WireMail.model_validate({"sensitivity": "private"})


async def test_mail_pagination_must_make_progress() -> None:
    from types import SimpleNamespace

    from eki.ingest.pipeline import _all_pages
    from eki.ingest.wire import WireMailPage

    class LoopingServer:
        calls = 0

        async def call_tool(self, *args: Any, **kwargs: Any) -> SimpleNamespace:
            self.calls += 1
            if self.calls > 1:
                raise AssertionError("pagination loop was not stopped")
            return SimpleNamespace(
                is_error=False, structured_content={"items": [], "total": 1, "next_offset": 0}
            )

    client: Any = LoopingServer()
    with pytest.raises(IngestError, match="pagination"):
        await _all_pages(client, "list_messages", WireMailPage)
