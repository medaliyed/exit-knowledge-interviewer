"""Opt-in real retrieval/selector checks. No calls without configured models and keys.

Run: uv run pytest -m live -s -rs tests/test_live_retrieval.py
Provider credentials must be explicitly exported to the test process; a .env
entry alone is not treated as consent for paid test calls.
"""

import os

import pytest
from pydantic import SecretStr

from eki.config import Settings, get_settings
from eki.search.answers import build_answers
from eki.search.service import build_search
from tests.conftest import Scratch
from tests.search_support import READER, search_world

pytestmark = [pytest.mark.live, pytest.mark.anyio]


def live_settings(scratch: Scratch, role: str) -> Settings:
    settings = get_settings()
    model = settings.model_embedding if role == "embedding" else settings.model_answerer
    if not model:
        pytest.skip(f"EKI_MODEL_{role.upper()} is not configured; no provider call made")
    keys = (
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "GEMINI_API_KEY",
        "MISTRAL_API_KEY",
        "AZURE_API_KEY",
        "COHERE_API_KEY",
        "AWS_SECRET_ACCESS_KEY",
    )
    if not any(os.environ.get(key) for key in keys):
        pytest.skip("no explicit provider credential in environment; no provider call made")
    # All application/audit data stays in the disposable schema, including provider audits.
    return settings.model_copy(
        update={
            "database_url": SecretStr(scratch.dsn),
            "langfuse_public_key": None,
            "langfuse_secret_key": None,
        }
    )


async def test_live_semantic_retrieval_handles_operator_paraphrases(scratch: Scratch) -> None:
    settings = live_settings(scratch, "embedding")
    world = await search_world(scratch)
    service = build_search(settings)
    for query, expected in (
        ("Undo the end-of-month balancing operation", world.billing),
        ("Replace the expiring gateway TLS credential after the vendor agrees", world.gateway),
    ):
        hits = service.search(READER, query)
        assert hits and hits[0].entry.id == str(expected), query
        assert all(h.entry.status == "approved" for h in hits)


async def test_live_selector_returns_grounded_rollback_and_abstains(scratch: Scratch) -> None:
    settings = live_settings(scratch, "answerer")
    world = await search_world(scratch)
    service = build_answers(settings, build_search(settings))
    result = await service.ask(READER, "How do I roll back month-end reconciliation?")
    assert result.status == "answered"
    assert "Restore the reconciliation table from the pre-run export" in result.answer
    assert result.evidence and all(
        e.entry_id == str(world.billing) and e.section == "rollback" and e.citations
        for e in result.evidence
    )
    missing = await service.ask(READER, "How do I fix a volcano rocket?")
    assert missing.status == "insufficient_evidence" and not missing.evidence
