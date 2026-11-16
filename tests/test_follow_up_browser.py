"""F22: optional real-browser acceptance against FastAPI, Postgres and scripted models.

Install the existing frontend development dependencies and Chrome/Playwright Chromium
to select this database test. No REST/SSE response is mocked and no provider is used.
"""

import json
import os
import re
import shutil
import socket
import subprocess
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from threading import Thread
from typing import Any

import pytest
import uvicorn

from eki import accounts
from eki.api.app import create_app
from eki.auth import TokenAuthenticator
from eki.config import Settings
from eki.llm import Message, Reply, ToolSpec
from tests.conftest import Scratch
from tests.fakes import ScriptedModel, tool_reply
from tests.followup_support import ROLLBACK_ANSWER, gap_world
from tests.interview_support import make_service
from tests.search_support import OWNER, READER

ROOT = Path(__file__).resolve().parents[1]
PASSWORD = "correct horse battery staple 42"


def rollback_interviewer(messages: Sequence[Message], tools: Sequence[ToolSpec]) -> Reply:
    """Ask each approved rollback seed and copy the synthetic answer verbatim."""
    seeds = re.findall(r"\d+\. \[gap_id=([^\]]+)\] (.+)", messages[1].content)
    assert seeds, "the real workflow must supply its approved follow-up seeds"
    asked: dict[str, str] = {}
    for message in messages:
        for call in message.tool_calls:
            if call.name == "ask_expert":
                asked[call.id] = str(call.arguments["gap_id"])
    calls: list[tuple[str, dict[str, Any]]] = []
    last = messages[-1]
    if last.role == "tool" and last.tool_call_id in asked:
        assert "The expert answered:" in last.content
        assert ROLLBACK_ANSWER in last.content, (
            "the actual accepted browser answer must reach the tool"
        )
        calls.append(("record_note", {"section": "rollback", "text": ROLLBACK_ANSWER}))
    pending = [(gap, text) for gap, text in seeds if gap not in asked.values()]
    if pending:
        gap, text = pending[0]
        calls.append(("ask_expert", {"question": text, "section": "rollback", "gap_id": gap}))
    else:
        calls.append(("finish_topic", {}))
    return tool_reply(*calls)


@pytest.mark.db
@pytest.mark.anyio
async def test_follow_up_through_real_browser_and_api(scratch: Scratch, tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None or not (ROOT / "frontend/node_modules/@playwright/test").exists():
        pytest.skip("optional browser acceptance needs npm ci in frontend/ and Node")
    world = await gap_world(scratch)
    with scratch.connect() as conn:
        for identity in (OWNER, READER):
            account = accounts.create_account(
                conn,
                email=f"{identity.subject}@example.com",
                name=identity.name,
                subject=identity.subject,
                work_domains=frozenset({"example.com"}),
            )
            accounts.set_password(conn, account.id, PASSWORD, now=datetime.now(UTC))
    model = ScriptedModel(rollback_interviewer)
    service = make_service(scratch.dsn, agent=None, interviewer=model)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(128)
        origin = f"http://127.0.0.1:{listener.getsockname()[1]}"
        app = create_app(
            service,
            authenticator=TokenAuthenticator("[]"),
            settings=Settings(
                _env_file=None,
                database_url=scratch.dsn,
                public_origin=origin,
                session_cookie_secure=False,
            ),
        )
        server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
        thread = Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 15
            while not server.started:
                assert thread.is_alive() and time.monotonic() < deadline, "API failed to start"
                time.sleep(0.05)
            result = subprocess.run(
                [node, "tests/follow_up_browser.cjs"],
                cwd=ROOT,
                env={
                    **os.environ,
                    "EKI_BROWSER_CASE": json.dumps(
                        {
                            "origin": origin,
                            "run": str(world.run_id),
                            "entry": str(world.billing),
                            "parent": str(world.session_id),
                            "password": PASSWORD,
                            "answer": ROLLBACK_ANSWER,
                            "screenshots": str(tmp_path),
                        }
                    ),
                },
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
            assert result.returncode == 0, result.stdout + result.stderr
            assert len(model.calls) >= 2, "the browser must drive the real interviewer workflow"
            with scratch.connect() as conn:
                assert conn.execute(
                    "SELECT count(*) FROM follow_up_gaps WHERE run_id=%s AND status='resolved'",
                    (world.run_id,),
                ).fetchone() == (2,)
        finally:
            server.should_exit = True
            thread.join(timeout=15)
            assert not thread.is_alive(), "the browser test's API must stop before schema cleanup"
