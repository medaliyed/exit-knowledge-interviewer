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
from tests.support import FAKE_CREDENTIALS

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
    response = client.post(
        "/runbook/search", json={"query": "reconciliation"}, headers=headers("reader")
    )
    assert response.status_code == 200, response.text
    assert [UUID(h["id"]) for h in response.json()["results"]] == [first.billing]
    assert client.post("/runbook/search", json={"query": "reconciliation"}).status_code == 401
    assert (
        client.post(
            "/runbook/search",
            json={"query": "reconciliation", "limit": 0},
            headers=headers("reader"),
        ).status_code
        == 422
    )
    # The query never travels in a URL: the old form is refused before any search.
    refused = client.get(
        "/runbook/search", params={"q": "reconciliation"}, headers=headers("reader")
    )
    assert refused.status_code == 405
    leak = "password=" + FAKE_CREDENTIALS[1]
    invalid = client.post(
        "/runbook/search", json={"query": leak, "extra": leak}, headers=headers("reader")
    )
    assert invalid.status_code == 422 and FAKE_CREDENTIALS[1] not in invalid.text


async def test_provider_failure_cannot_leave_a_partially_indexed_entry(scratch: Scratch) -> None:
    from collections.abc import Sequence

    from eki.ingest.embedding import HashingEmbedder
    from eki.ingest.redaction import RedactedText
    from eki.llm import ModelError

    world = await search_world(scratch)
    books = RunbookService(scratch.dsn)
    books.edit(world.gateway, OWNER.name, {"steps": SectionEdit(claims=("Now a draft",))})
    books.edit(
        world.billing,
        OWNER.name,
        {
            "steps": SectionEdit(
                claims=tuple("Reconcile entry " + "the totals " * 60 + str(n) for n in range(130))
            )
        },
    )
    books.approve(world.billing, OWNER.name)

    class FlakyEmbedder(HashingEmbedder):
        name = "test-failing-batches"
        calls = 0
        fail = True

        def embed(self, texts: Sequence[RedactedText]) -> list[list[float]]:
            self.calls += 1
            if self.fail and self.calls == 2:
                raise ModelError("simulated provider failure")
            return super().embed(texts)

    embedder = FlakyEmbedder()
    service = SearchService(scratch.dsn, embedder=embedder)
    with pytest.raises(ModelError):
        service.search(READER, "reconcile")
    with scratch.connect() as conn:
        assert conn.execute(
            "SELECT count(*) FROM runbook_index WHERE entry_id = %s", (world.billing,)
        ).fetchone() == (0,)
    embedder.fail = False
    assert service.search(READER, "reconcile")
    with scratch.connect() as conn:
        count = conn.execute(
            "SELECT count(*) FROM runbook_index WHERE entry_id = %s AND section = 'steps'",
            (world.billing,),
        ).fetchone()
        assert count is not None and count[0] == 130


async def test_long_entry_cannot_starve_other_matching_entries(scratch: Scratch) -> None:
    from collections.abc import Sequence

    from eki.ingest.redaction import RedactedText
    from eki.runbook import store
    from eki.runbook.models import SECTION_NAMES

    world = await search_world(scratch)
    books = RunbookService(scratch.dsn)
    long_id, short_id = sorted((world.billing, world.gateway))
    phrase = "gateway certificate renewal " * 20
    for entry_id, size in ((long_id, 500), (short_id, 1)):
        edited = books.edit(
            entry_id, OWNER.name, {s: SectionEdit(open_question="Not known") for s in SECTION_NAMES}
        )
        edited = books.edit(entry_id, OWNER.name, {"steps": SectionEdit(claims=(phrase,) * size)})
        with scratch.connect() as conn:
            store.save_entry(
                conn,
                world.session_id,
                edited.entry.model_copy(update={"title": "Gateway certificate renewal"}),
            )
        books.approve(entry_id, OWNER.name)

    class SameVectors:
        name = "test-equal-vectors"
        dimensions = 256

        def embed(self, texts: Sequence[RedactedText]) -> list[list[float]]:
            return [[1.0] + [0.0] * 255 for _ in texts]

    hits = SearchService(scratch.dsn, embedder=SameVectors()).search(
        READER, "gateway certificate renewal", limit=2
    )
    assert {UUID(hit.entry.id) for hit in hits} == {long_id, short_id}
