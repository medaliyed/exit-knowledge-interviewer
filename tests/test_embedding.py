"""Deliverable g: the local hashing embedder is deterministic, normalised and meaningful."""

import json
import math
import os
import subprocess
import sys

from eki.ingest.embedding import Embedder, HashingEmbedder, to_vector_literal
from eki.ingest.redaction import RedactedText


def text(value: str) -> RedactedText:
    return RedactedText(value)


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


def test_it_satisfies_the_embedder_protocol() -> None:
    embedder: Embedder = HashingEmbedder()
    assert embedder.dimensions == 256
    (vector,) = embedder.embed([text("hello")])
    assert len(vector) == 256


def test_embedding_is_unit_length() -> None:
    (vector,) = HashingEmbedder().embed([text("restart the ledger-db replicas first")])
    assert math.isclose(math.sqrt(sum(x * x for x in vector)), 1.0, rel_tol=1e-9)


def test_empty_text_gives_zero_vector() -> None:
    (vector,) = HashingEmbedder().embed([text("")])
    assert vector == [0.0] * 256
    (punctuation,) = HashingEmbedder().embed([text("!!! ??? ...")])
    assert punctuation == [0.0] * 256


def test_similar_texts_are_closer_than_unrelated_ones() -> None:
    a, b, c = HashingEmbedder().embed(
        [
            text("Gateway TLS certificate renewal"),
            text("gateway certificate renewal for the partner pool"),
            text("Add a regional breakdown to the tax export"),
        ]
    )
    assert cosine(a, b) > 0.5
    assert cosine(a, b) > cosine(a, c) + 0.3


def test_case_and_punctuation_do_not_matter() -> None:
    a, b = HashingEmbedder().embed([text("Billing-Batch FAILED!"), text("billing batch failed")])
    assert math.isclose(cosine(a, b), 1.0, rel_tol=1e-9)


def test_embedding_is_deterministic_across_processes() -> None:
    sentence = "restart the ledger-db replicas, then re-run billing-batch"
    here = HashingEmbedder().embed([text(sentence)])[0]
    code = (
        "import json, sys; from eki.ingest.embedding import HashingEmbedder; "
        "from eki.ingest.redaction import RedactedText; "
        f"print(json.dumps(HashingEmbedder().embed([RedactedText({sentence!r})])[0]))"
    )
    for seed in ("1", "2", "random"):
        env = {**os.environ, "PYTHONHASHSEED": seed}
        out = subprocess.run(
            [sys.executable, "-c", code], env=env, capture_output=True, text=True, check=True
        )
        assert json.loads(out.stdout) == here


def test_vector_literal_format() -> None:
    literal = to_vector_literal([0.5, -0.25, 0.0])
    assert literal == "[0.500000,-0.250000,0.000000]"
