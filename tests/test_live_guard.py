"""Live validation never silently chooses a model or starts a paid provider call."""

from types import SimpleNamespace
from typing import Any

import pytest

from tests.test_live_retrieval import live_settings


@pytest.mark.parametrize("role", ["embedding", "answerer"])
def test_live_checks_skip_before_any_work_without_model(
    monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    settings = SimpleNamespace(model_embedding=None, model_answerer=None)
    monkeypatch.setattr("tests.test_live_retrieval.get_settings", lambda: settings)
    scratch: Any = SimpleNamespace(dsn="unused")
    with pytest.raises(pytest.skip.Exception, match="not configured"):
        live_settings(scratch, role)


def test_live_checks_skip_without_explicit_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = SimpleNamespace(model_embedding="test/model", model_answerer="test/model")
    monkeypatch.setattr("tests.test_live_retrieval.get_settings", lambda: settings)
    for key in (
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "GEMINI_API_KEY",
        "MISTRAL_API_KEY",
        "AZURE_API_KEY",
        "COHERE_API_KEY",
        "AWS_SECRET_ACCESS_KEY",
    ):
        monkeypatch.delenv(key, raising=False)
    scratch: Any = SimpleNamespace(dsn="unused")
    with pytest.raises(pytest.skip.Exception, match="no explicit provider credential"):
        live_settings(scratch, "embedding")
