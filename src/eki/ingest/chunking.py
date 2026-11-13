"""Split redacted text into chunks for embedding, on paragraph boundaries."""

from eki.ingest.redaction import RedactedText


def _split_long(paragraph: str, max_chars: int) -> list[str]:
    """Break one long paragraph on whitespace. A word longer than the limit stays whole."""
    pieces: list[str] = []
    current: list[str] = []
    length = 0
    for word in paragraph.split():
        added = len(word) + (1 if current else 0)
        if current and length + added > max_chars:
            pieces.append(" ".join(current))
            current, length = [word], len(word)
        else:
            current.append(word)
            length += added
    if current:
        pieces.append(" ".join(current))
    return pieces


def chunk(text: RedactedText, max_chars: int = 1200) -> tuple[RedactedText, ...]:
    """Pack paragraphs into chunks of at most ``max_chars``, never splitting a word.

    A paragraph is never split unless it alone exceeds the limit. Empty input gives ``()``.
    """
    chunks: list[str] = []
    current = ""
    for paragraph in (p.strip() for p in text.split("\n\n")):
        if not paragraph:
            continue
        for piece in (
            _split_long(paragraph, max_chars) if len(paragraph) > max_chars else [paragraph]
        ):
            if current and len(current) + 2 + len(piece) <= max_chars:
                current = f"{current}\n\n{piece}"
            else:
                if current:
                    chunks.append(current)
                current = piece
    if current:
        chunks.append(current)
    return tuple(RedactedText(c) for c in chunks)
