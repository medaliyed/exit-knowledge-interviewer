"""F3: explicit consent, private mail exclusion, fixed scope and read-only tools."""

import json
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest
from mcp import Client

from mcp_servers.common.source import SourceAccessError
from mcp_servers.mail_server import build_server
from tests.support import CORPUS, FAKE_CREDENTIALS, FULL_SPAN, tree_hashes

pytestmark = [pytest.mark.anyio, pytest.mark.redteam]


@pytest.mark.parametrize(
    "folders",
    [
        (),
        ("Personal",),
        ("private",),
        ("Inbox/../Personal",),
        ("/Inbox",),
        ("Inbox\\Personal",),
        ("Work/Personal Archive",),
    ],
)
async def test_mail_scope_requires_safe_explicit_folders(folders: tuple[str, ...]) -> None:
    with pytest.raises((ValueError, SourceAccessError)):
        build_server(replace(FULL_SPAN, mail_folders=folders), CORPUS)


async def test_only_selected_normal_expert_messages_are_returned() -> None:
    scope = replace(FULL_SPAN, mail_folders=("Inbox",))
    before = tree_hashes(CORPUS)
    async with Client(build_server(scope, CORPUS), raise_exceptions=True) as client:
        tools = (await client.list_tools()).tools
        assert {t.name for t in tools} == {"list_messages", "get_message"}
        assert all(t.annotations is not None and t.annotations.read_only_hint for t in tools)
        page = await client.call_tool("list_messages", {"limit": 200})
        data = page.structured_content
        assert data is not None and data["items"]
        returned = data["items"]
        expected = [
            json.loads(path.read_text()) for path in (CORPUS / "mail" / "Inbox").glob("*.json")
        ]
        expected = [
            m
            for m in expected
            if m["sensitivity"] == "normal"
            and scope.expert_email in [m["from"], *m["to"], *m["cc"]]
            and scope.contains(datetime.fromisoformat(m["sent_at"].replace("Z", "+00:00")))
        ]
        assert {m["id"] for m in returned} == {m["id"] for m in expected}
        assert all(m["folder"] == "Inbox" and m["sensitivity"] == "normal" for m in returned)
        assert all(secret not in str(returned) for secret in FAKE_CREDENTIALS)
        for identifier in ("EML-0008", "../Personal/EML-0008", "unknown"):
            assert (await client.call_tool("get_message", {"message_id": identifier})).is_error
        # The SDK ignores unknown arguments; they still cannot widen the fixed scope.
        extra = await client.call_tool("list_messages", {"folder": "Personal"})
        assert extra.structured_content is not None
        assert all(m["folder"] == "Inbox" for m in extra.structured_content["items"])
        assert (await client.call_tool("list_messages", {"limit": 0})).is_error
    assert tree_hashes(CORPUS) == before


async def test_selected_mail_folder_symlink_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "mail").mkdir()
    (tmp_path / "private").mkdir()
    (tmp_path / "mail" / "Inbox").symlink_to(tmp_path / "private", target_is_directory=True)
    with pytest.raises((ValueError, SourceAccessError)):
        build_server(replace(FULL_SPAN, mail_folders=("Inbox",)), tmp_path)
