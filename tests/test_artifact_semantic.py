"""F17: artifact index model identity, atomic replacement and semantic query plumbing."""

from collections.abc import Sequence
from uuid import uuid4

import pytest

from eki.ingest.embedding import HashingEmbedder
from eki.ingest.index import index_run, search_chunks
from eki.ingest.pipeline import store
from eki.ingest.redaction import RedactedText
from eki.llm import ModelError
from tests import builders
from tests.conftest import Scratch

pytestmark = pytest.mark.db


class SemanticDouble(HashingEmbedder):
    name = "test-artifact-semantic-v1"

    def embed(self, texts: Sequence[RedactedText]) -> list[list[float]]:
        result = []
        for text in texts:
            lowered = text.lower()
            index = (
                0
                if any(t in lowered for t in ("certificate", "credential"))
                else (1 if any(t in lowered for t in ("replica", "standby")) else 2)
            )
            vector = [0.0] * 256
            vector[index] = 1.0
            result.append(vector)
        return result


def test_artifact_paraphrases_use_the_recorded_model(scratch: Scratch) -> None:
    artifacts = [
        builders.ticket("CERT", "gateway", "Renew the gateway certificate."),
        builders.ticket("FAIL", "ledger", "Promote the database replica."),
        builders.ticket("RECON", "billing", "Undo month-end reconciliation."),
    ]
    embedder = SemanticDouble()
    with scratch.connect() as conn:
        run = store(conn, builders.snapshot(artifacts))
        index_run(conn, run, embedder)
        for query, expected in (
            ("Replace the expiring credential", "CERT"),
            ("Bring the standby into service", "FAIL"),
            ("Reverse the balancing operation", "RECON"),
        ):
            hits = search_chunks(conn, run, RedactedText(query), embedder)
            assert hits[0].artifact_id == expected
        with pytest.raises(ModelError, match="model"):
            search_chunks(conn, run, RedactedText("anything"), HashingEmbedder())
        assert search_chunks(conn, uuid4(), RedactedText("anything"), embedder) == []


def test_failed_second_batch_keeps_previous_index_and_model(scratch: Scratch) -> None:
    class Failing(SemanticDouble):
        calls = 0

        def embed(self, texts: Sequence[RedactedText]) -> list[list[float]]:
            self.calls += 1
            if self.calls == 2:
                raise ModelError("failed second batch")
            assert len(texts) <= 64
            return super().embed(texts)

    with scratch.connect() as conn:
        run = store(
            conn,
            builders.snapshot(
                [builders.ticket(f"T{i}", "gateway", "Certificate") for i in range(70)]
            ),
        )
        count = index_run(conn, run, HashingEmbedder())
        with pytest.raises(ModelError):
            index_run(conn, run, Failing())
        assert conn.execute("SELECT count(*) FROM chunks WHERE run_id=%s", (run,)).fetchone() == (
            count,
        )
        assert search_chunks(conn, run, RedactedText("certificate"), HashingEmbedder())


def test_invalid_vectors_cannot_replace_a_working_index(scratch: Scratch) -> None:
    class Invalid(SemanticDouble):
        def embed(self, texts: Sequence[RedactedText]) -> list[list[float]]:
            return [[1.0, 2.0] for _ in texts]

    with scratch.connect() as conn:
        run = store(conn, builders.snapshot([builders.ticket("A", "gateway", "Certificate")]))
        index_run(conn, run, HashingEmbedder())
        with pytest.raises(ModelError):
            index_run(conn, run, Invalid())
        assert search_chunks(conn, run, RedactedText("certificate"), HashingEmbedder())


def test_recorded_model_wins_over_changed_configuration(scratch: Scratch) -> None:
    from pydantic import SecretStr

    from eki.config import Settings
    from eki.ingest.semantic import configured, for_run

    settings = Settings(
        _env_file=None,
        database_url=SecretStr(scratch.dsn),
        model_artifact_embedding="new-provider/model",
    )
    with scratch.connect() as conn:
        run = store(conn, builders.snapshot([builders.ticket("A", "gateway", "Certificate")]))
        index_run(conn, run, HashingEmbedder())
        assert for_run(conn, run, settings).name == HashingEmbedder.name
        conn.execute(
            "UPDATE ingest_runs SET artifact_embedding_model=%s WHERE id=%s",
            ("litellm:previous-provider/model:256", run),
        )
        assert for_run(conn, run, settings).name == "litellm:previous-provider/model:256"
    assert configured(settings).name == "litellm:new-provider/model:256"


def test_artifact_provider_receives_redacted_text_and_scoped_audit(
    scratch: Scratch, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace
    from typing import Any

    from pydantic import SecretStr

    from eki.config import Settings
    from eki.ingest.semantic import configured
    from tests.support import FAKE_CREDENTIALS

    calls: list[list[str]] = []

    def embed(**kwargs: Any) -> SimpleNamespace:
        calls.append(kwargs["input"])
        return SimpleNamespace(
            data=[
                {"index": i, "embedding": [1.0] + [0.0] * 255} for i in range(len(kwargs["input"]))
            ]
        )

    monkeypatch.setattr("eki.search.embedding._litellm", lambda: SimpleNamespace(embedding=embed))
    settings = Settings(
        _env_file=None, database_url=SecretStr(scratch.dsn), model_artifact_embedding="test/model"
    )
    with scratch.connect() as conn:
        run = store(conn, builders.snapshot([builders.ticket("A", "gateway", "Certificate")]))
        embedder = configured(settings, run_id=run)
        index_run(conn, run, embedder)
        search_chunks(
            conn, run, RedactedText(f"certificate password={FAKE_CREDENTIALS[1]}"), embedder
        )
        events = conn.execute(
            "SELECT run_id, details FROM audit_events WHERE kind='model'"
        ).fetchall()
    assert len(events) == 4 and all(row[0] == run for row in events)
    assert FAKE_CREDENTIALS[1] not in str(calls) + str(events)
    assert "[REDACTED:credential]" in str(calls)


def test_reindex_race_is_detected_before_vector_comparison(scratch: Scratch) -> None:
    change = {"now": False}
    with scratch.connect() as conn:

        class Racing(SemanticDouble):
            def embed(self, texts: Sequence[RedactedText]) -> list[list[float]]:
                result = super().embed(texts)
                if change["now"]:
                    index_run(conn, run, HashingEmbedder())
                return result

        run = store(conn, builders.snapshot([builders.ticket("A", "gateway", "Certificate")]))
        semantic = Racing()
        index_run(conn, run, semantic)
        change["now"] = True
        with pytest.raises(ModelError, match="changed"):
            search_chunks(conn, run, RedactedText("credential"), semantic)
        assert search_chunks(conn, run, RedactedText("certificate"), HashingEmbedder())
