"""Deliverable g: text is split into chunks on paragraph boundaries, never mid-word."""

from eki.ingest.chunking import chunk
from eki.ingest.redaction import RedactedText


def text(value: str) -> RedactedText:
    return RedactedText(value)


def test_short_text_is_one_chunk() -> None:
    assert chunk(text("one short paragraph")) == ("one short paragraph",)


def test_empty_text_gives_no_chunks() -> None:
    assert chunk(text("")) == ()
    assert chunk(text("  \n\n  ")) == ()


def test_long_text_splits_on_paragraphs_within_limit() -> None:
    paragraphs = [f"paragraph {n} " + "word " * 40 for n in range(10)]
    chunks = chunk(text("\n\n".join(paragraphs)), max_chars=500)
    assert len(chunks) > 1
    assert all(len(c) <= 500 for c in chunks)
    # every chunk starts at a paragraph boundary
    assert all(c.startswith("paragraph") for c in chunks)


def test_chunks_rejoin_to_the_original_words() -> None:
    original = "\n\n".join(f"alpha{n} " + "lorem ipsum dolor " * 30 for n in range(8))
    chunks = chunk(text(original), max_chars=400)
    assert " ".join(" ".join(chunks).split()) == " ".join(original.split())


def test_one_huge_paragraph_is_split_on_whitespace() -> None:
    chunks = chunk(text("word " * 1000), max_chars=300)
    assert all(len(c) <= 300 for c in chunks)
    assert all(c.split() and set(c.split()) == {"word"} for c in chunks)


def test_a_single_word_longer_than_the_limit_is_kept_whole() -> None:
    giant = "x" * 50
    assert chunk(text(f"before {giant} after"), max_chars=10) == ("before", giant, "after")


def test_chunking_is_deterministic() -> None:
    sample = text("\n\n".join(f"p{n} " + "w " * 100 for n in range(20)))
    assert chunk(sample, max_chars=300) == chunk(sample, max_chars=300)
