"""Optional semantic embeddings behind the existing redacted-text boundary."""

import math
from collections.abc import Sequence
from typing import Protocol

from eki.config import Settings, work_domains
from eki.ingest.embedding import Embedder, HashingEmbedder
from eki.ingest.redaction import RedactedText, redact
from eki.llm import ModelError
from eki.llm.litellm_model import _litellm


class SearchEmbedder(Embedder, Protocol):
    name: str


def checked_vectors(vectors: list[list[float]], count: int) -> list[list[float]]:
    if len(vectors) != count or any(
        len(v) != 256 or any(not math.isfinite(x) for x in v) for v in vectors
    ):
        raise ModelError("embedding provider returned invalid vectors")
    return vectors


class LiteLLMEmbedder:
    dimensions = 256

    def __init__(self, model: str, timeout: float, domains: frozenset[str]) -> None:
        self.model, self.timeout, self.domains = model, timeout, domains
        self.name = f"litellm:{model}:256"

    def embed(self, texts: Sequence[RedactedText]) -> list[list[float]]:
        if not texts:
            return []
        try:
            response = _litellm().embedding(
                model=self.model,
                input=[str(redact(t, work_email_domains=self.domains).text) for t in texts],
                dimensions=256,
                timeout=self.timeout,
                encoding_format="float",
            )
            data = sorted(response.data, key=lambda item: item["index"])
            if [item["index"] for item in data] != list(range(len(texts))):
                raise ValueError
            vectors = [[float(x) for x in item["embedding"]] for item in data]
            return checked_vectors(vectors, len(texts))
        except Exception as error:
            raise ModelError("embedding provider failed or returned invalid vectors") from error


def build_embedder(settings: Settings) -> SearchEmbedder:
    if settings.model_embedding:
        return LiteLLMEmbedder(
            settings.model_embedding, settings.model_timeout_seconds, work_domains(settings)
        )
    return HashingEmbedder()
