"""F17: opt-in real artifact paraphrase checks; never run by default or CI."""

import pytest
from pydantic import SecretStr

from eki.config import get_settings
from eki.ingest.embedding import HashingEmbedder
from eki.ingest.index import index_run, search_chunks
from eki.ingest.pipeline import store
from eki.ingest.redaction import RedactedText
from eki.ingest.semantic import configured
from tests import builders
from tests.conftest import Scratch
from tests.live_support import has_provider_key

pytestmark = pytest.mark.live


@pytest.mark.parametrize("attempt", [1, 2, 3])
def test_live_artifact_paraphrases(scratch: Scratch, attempt: int) -> None:
    settings = get_settings()
    if not settings.model_artifact_embedding:
        pytest.skip("EKI_MODEL_ARTIFACT_EMBEDDING is not configured; no provider call made")
    if not has_provider_key():
        pytest.skip("no explicit provider credential in environment; no provider call made")
    settings = settings.model_copy(update={"database_url": SecretStr(scratch.dsn)})
    artifacts = [
        builders.ticket(
            "CERT", "gateway", "Gateway TLS certificate renewal after vendor confirmation"
        ),
        builders.ticket(
            "FAIL", "ledger", "Promote the ledger database replica after stopping the writer"
        ),
        builders.ticket(
            "RECON", "billing", "Month-end reconciliation rollback: restore table from export"
        ),
        *[
            builders.ticket(f"DISTRACTOR-{i}", "billing", f"Add regional sales export column {i}")
            for i in range(8)
        ],
    ]
    cases = (
        ("Replace an expiring TLS credential", "CERT"),
        ("Bring the database standby into service", "FAIL"),
        ("Undo the end-of-month balancing operation", "RECON"),
    )
    with scratch.connect() as conn:
        run = store(conn, builders.snapshot(artifacts))
        offline = HashingEmbedder()
        index_run(conn, run, offline)
        baseline = sum(
            expected
            in {h.artifact_id for h in search_chunks(conn, run, RedactedText(query), offline, k=5)}
            for query, expected in cases
        )
        print(f"Artifact retrieval attempt {attempt}: offline top-5 {baseline}/3")
        embedder = configured(settings, run_id=run)
        index_run(conn, run, embedder)
        for query, expected in cases:
            hits = search_chunks(conn, run, RedactedText(query), embedder, k=5)
            assert expected in {h.artifact_id for h in hits}, (attempt, query)

    print(f"Artifact retrieval attempt {attempt}: semantic top-5 3/3")
