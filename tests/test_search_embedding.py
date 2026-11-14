"""F11: provider requests are redacted, vectors validated and model spaces isolated."""

from types import SimpleNamespace
from typing import Any

import pytest

from eki.ingest.redaction import RedactedText
from eki.llm import ModelError
from eki.search.embedding import LiteLLMEmbedder
from tests.support import FAKE_CREDENTIALS


def test_provider_redacts_inputs_and_orders_vectors(monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[dict[str, Any]] = []

    def embedding(**kwargs: Any) -> SimpleNamespace:
        requests.append(kwargs)
        return SimpleNamespace(
            data=[{"index": 1, "embedding": [0.0] * 256}, {"index": 0, "embedding": [1.0] * 256}]
        )

    monkeypatch.setattr(
        "eki.search.embedding._litellm", lambda: SimpleNamespace(embedding=embedding)
    )
    model = LiteLLMEmbedder("test/semantic", 3, frozenset())
    result = model.embed([RedactedText(f"password={FAKE_CREDENTIALS[1]}"), RedactedText("hello")])
    assert result[0] == [1.0] * 256
    assert FAKE_CREDENTIALS[1] not in str(requests)
    assert requests[0]["dimensions"] == 256 and requests[0]["timeout"] == 3
    assert model.name == "litellm:test/semantic:256"


@pytest.mark.parametrize(
    "data",
    [
        [{"index": 0, "embedding": [1.0]}],
        [{"index": 0, "embedding": [float("nan")] * 256}],
        [{"index": 2, "embedding": [0.0] * 256}],
        [],
    ],
)
def test_invalid_provider_vectors_are_refused(
    monkeypatch: pytest.MonkeyPatch, data: list[dict[str, Any]]
) -> None:
    monkeypatch.setattr(
        "eki.search.embedding._litellm",
        lambda: SimpleNamespace(embedding=lambda **kw: SimpleNamespace(data=data)),
    )
    with pytest.raises(ModelError):
        LiteLLMEmbedder("test/provider", 3, frozenset()).embed([RedactedText("text")])
