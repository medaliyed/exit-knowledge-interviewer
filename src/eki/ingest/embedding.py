"""Embeddings.

``Embedder`` is the interface everything else uses. Phase 1 ships one local,
deterministic implementation so that pgvector is exercised fully with no network
and no key. A real model arrives in phase 2 behind the same interface.

Both ``embed`` and the search function accept only ``RedactedText``, so mypy rejects
a call that passes raw text.
"""

import hashlib
import math
import re
from collections.abc import Sequence
from typing import Protocol

from eki.ingest.redaction import RedactedText

TOKEN = re.compile(r"[a-z0-9]+")


class Embedder(Protocol):
    dimensions: int

    def embed(self, texts: Sequence[RedactedText]) -> list[list[float]]: ...


class HashingEmbedder:
    """Signed feature hashing of lowercase word tokens, L2-normalised.

    Uses ``hashlib.blake2b``, not Python's ``hash()``, which differs between processes.
    Text with no word tokens embeds to the zero vector.
    """

    name = "hashing-blake2b-v1-256"
    dimensions = 256

    def embed(self, texts: Sequence[RedactedText]) -> list[list[float]]:
        return [self._embed_one(text) for text in texts]

    def _embed_one(self, text: str) -> list[float]:
        vector = [0.0] * self.dimensions
        for token in TOKEN.findall(text.lower()):
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
            bucket = int.from_bytes(digest[:4], "big") % self.dimensions
            vector[bucket] += 1.0 if digest[4] & 1 else -1.0
        norm = math.sqrt(sum(value * value for value in vector))
        return [value / norm for value in vector] if norm else vector


def to_vector_literal(vector: Sequence[float]) -> str:
    """The text form pgvector accepts, for use with ``%s::vector``."""
    return "[" + ",".join(f"{value:.6f}" for value in vector) + "]"
