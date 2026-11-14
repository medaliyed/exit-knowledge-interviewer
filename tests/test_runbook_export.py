"""F12: complete cited Markdown with safe rendering and live access checks."""

from datetime import UTC, datetime

import pytest

from eki.runbook.export import ExportService, render_markdown
from eki.runbook.models import (
    SECTION_NAMES,
    Claim,
    EntryStatus,
    InterviewTurnCitation,
    RunbookEntry,
    Section,
)
from eki.runbook.review import SectionEdit
from eki.runbook.service import RunbookService
from tests.conftest import Scratch
from tests.search_support import OWNER, READER, STRANGER, search_world


def test_render_complete_template_and_escape_untrusted_text() -> None:
    citation = InterviewTurnCitation(session_id="session", turn_index=2)
    section = Section(
        claims=(
            Claim(
                text="<script>alert(1)</script> [click](javascript:bad)\n# Heading",
                citations=(citation,),
            ),
        )
    )
    entry = RunbookEntry.model_validate(
        {
            "id": "entry",
            "topic_id": "topic",
            "title": "Unsafe <title>",
            "status": EntryStatus.APPROVED,
            "approved_by": "Nadia",
            "approved_at": datetime.now(UTC),
            **{s: section for s in SECTION_NAMES},
        }
    )
    markdown = render_markdown(entry, version=3)
    assert "<script>" not in markdown and "&lt;script&gt;" in markdown
    assert "[click](javascript:" not in markdown
    assert "version: 3" in markdown.lower()
    for name in SECTION_NAMES:
        assert f"## {name.title()}" in markdown
    assert "turn 2" in markdown
    assert markdown.count("]: Interview") == 1, "identical citations share a footnote"
    with pytest.raises(ValueError):
        render_markdown(
            entry.model_copy(
                update={"status": EntryStatus.DRAFT, "approved_by": None, "approved_at": None}
            ),
            version=3,
        )


@pytest.mark.db
@pytest.mark.anyio
async def test_exports_are_approved_and_access_filtered(scratch: Scratch) -> None:
    from fastapi.testclient import TestClient

    from eki.api.app import create_app
    from tests.test_api_access import authenticator, headers

    world = await search_world(scratch)
    service = ExportService(scratch.dsn)
    doc = service.entry(READER, world.billing)
    assert "reconciliation" in doc and "[^" in doc
    for principal, entry in (
        (STRANGER, world.billing),
        (OWNER, world.draft),
        (READER, world.draft),
    ):
        from eki.errors import NotFoundError

        with pytest.raises(NotFoundError):
            service.entry(principal, entry)
    bulk = service.run(READER, world.run_id)
    assert "Gateway certificate" in bulk and "Unapproved payroll" not in bulk
    client = TestClient(
        create_app(runbook_service=RunbookService(scratch.dsn), authenticator=authenticator())
    )
    response = client.get(f"/entries/{world.billing}/markdown", headers=headers("reader"))
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/markdown")
    assert "attachment" in response.headers["content-disposition"]
    assert "no-store" in response.headers["cache-control"]
    assert (
        client.get(f"/entries/{world.draft}/markdown", headers=headers("owner")).status_code == 404
    )
    assert (
        client.get(
            "/runbook/export", params={"run_id": str(world.run_id)}, headers=headers("reader")
        ).status_code
        == 200
    )
    books = RunbookService(scratch.dsn)
    books.edit(world.billing, OWNER.name, {"steps": SectionEdit(claims=("Unapproved change",))})
    assert (
        client.get(f"/entries/{world.billing}/markdown", headers=headers("reader")).status_code
        == 404
    )
