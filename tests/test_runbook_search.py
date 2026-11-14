"""F11: filtered hybrid search, freshness, stable relevance and safe embeddings."""

from uuid import UUID

import pytest

from eki import access
from eki.runbook.models import InterviewTurnCitation
from eki.runbook.review import SectionEdit
from eki.runbook.service import RunbookService
from eki.search.service import SearchService
from tests.conftest import Scratch
from tests.interview_support import make_service, say
from tests.search_support import OWNER, READER, STRANGER, search_world

pytestmark = [pytest.mark.db, pytest.mark.anyio]


async def test_relevance_and_visibility(scratch: Scratch) -> None:
    world = await search_world(scratch)
    service = SearchService(scratch.dsn)
    for query, expected in (
        ("reconciliation rollback", world.billing),
        ("gateway certificate", world.gateway),
    ):
        hits = service.search(READER, query)
        assert hits and UUID(hits[0].entry.id) == expected
        assert all(h.entry.status == "approved" for h in hits)
        assert world.draft not in [UUID(h.entry.id) for h in hits]
        assert service.search(READER, query) == hits
    assert service.search(STRANGER, "reconciliation") == []
    assert service.search(READER, "volcano kangaroo rocket") == []
    assert service.search(READER, "the and or") == []
    for query in ("", " ", "x" * 2001):
        with pytest.raises(ValueError):
            service.search(READER, query)
    for limit in (0, 51):
        with pytest.raises(ValueError):
            service.search(READER, "reconciliation", limit=limit)


async def test_edit_correction_and_acl_revocation_remove_results(scratch: Scratch) -> None:
    world = await search_world(scratch)
    search = SearchService(scratch.dsn)
    books = RunbookService(scratch.dsn)
    assert search.search(READER, "reconciliation")
    books.edit(world.billing, OWNER.name, {"steps": SectionEdit(claims=("Changed procedure.",))})
    assert world.billing not in [UUID(h.entry.id) for h in search.search(READER, "reconciliation")]
    books.approve(world.billing, OWNER.name)
    assert search.search(READER, "reconciliation")
    entry = books.get(world.billing).entry
    citation = entry.steps.claims[0].citations[0]
    assert isinstance(citation, InterviewTurnCitation)
    await say(
        make_service(scratch.dsn),
        world.session_id,
        "Corrected steps.",
        action="correct",
        corrects=citation.turn_index,
    )
    assert world.billing not in [UUID(h.entry.id) for h in search.search(READER, "reconciliation")]
    with scratch.connect() as conn:
        access.set_readers(conn, world.run_id, [], OWNER)
    assert search.search(READER, "gateway certificate") == []


async def test_vector_only_match_uses_the_configured_embedding_space(scratch: Scratch) -> None:
    from collections.abc import Sequence

    from eki.ingest.redaction import RedactedText

    class SemanticFake:
        name = "test-semantic-256"
        dimensions = 256

        def embed(self, texts: Sequence[RedactedText]) -> list[list[float]]:
            return [
                [1.0, 0.0] + [0.0] * 254
                if any(t in text.lower() for t in ("undo", "restore"))
                else [0.0, 1.0] + [0.0] * 254
                for text in texts
            ]

    world = await search_world(scratch)
    service = SearchService(scratch.dsn, embedder=SemanticFake())
    hits = service.search(READER, "undo")
    assert [UUID(h.entry.id) for h in hits] == [world.billing]
    assert hits[0].section == "rollback"
    # A different embedding model must build/use its own rows, not mix incompatible vectors.
    local = SearchService(scratch.dsn)
    assert UUID(local.search(READER, "gateway certificate")[0].entry.id) == world.gateway


async def test_search_api_and_cross_run_filtering(scratch: Scratch) -> None:
    from fastapi.testclient import TestClient

    from eki.api.app import create_app
    from tests.test_api_access import authenticator, headers

    first = await search_world(scratch)
    hidden = await search_world(scratch)
    with scratch.connect() as conn:
        access.set_readers(conn, hidden.run_id, [], OWNER)
    service = SearchService(scratch.dsn)
    client = TestClient(create_app(search_service=service, authenticator=authenticator()))
    response = client.get(
        "/runbook/search", params={"q": "reconciliation"}, headers=headers("reader")
    )
    assert response.status_code == 200, response.text
    assert [UUID(h["id"]) for h in response.json()["results"]] == [first.billing]
    assert client.get("/runbook/search", params={"q": "reconciliation"}).status_code == 401
    assert (
        client.get(
            "/runbook/search", params={"q": "reconciliation", "limit": 0}, headers=headers("reader")
        ).status_code
        == 422
    )
