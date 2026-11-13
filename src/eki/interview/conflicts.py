"""Find artifacts of one topic that contradict each other about an order of steps.

Deliberately narrow and rule-based: it only sees "X first, then Y" against
"Y first, then X". It never decides which side is right; it produces a question
for the expert that quotes both.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass

from eki.ingest.models import Artifact
from eki.ingest.redaction import RedactedText
from eki.interview.models import Question

_ORDER = re.compile(r"(?i)([^.:;\n]*?)\s+first,?\s+(?:and\s+)?then\s+([^.;\n]*)")
_WORD = re.compile(r"[a-z0-9]+")
_FILLER = frozenset({"the", "a", "an", "and", "then", "first", "to", "of", "restart", "restarted"})


@dataclass(frozen=True)
class Conflict:
    first_id: str
    first_quote: RedactedText
    second_id: str
    second_quote: RedactedText

    def question(self) -> Question:
        return Question(
            text=RedactedText(
                f'{self.first_id} says "{self.first_quote}", but {self.second_id} says '
                f'"{self.second_quote}". Which is right?'
            ),
            artifact_id=self.first_id,
            quote=self.first_quote,
        )


@dataclass(frozen=True)
class _Statement:
    artifact_id: str
    quote: RedactedText
    before: frozenset[str]
    after: frozenset[str]


def _words(text: str) -> frozenset[str]:
    return frozenset(_WORD.findall(text.lower())) - _FILLER


def _statements(artifact: Artifact) -> list[_Statement]:
    found = []
    for match in _ORDER.finditer(artifact.body):
        before, after = _words(match.group(1)), _words(match.group(2))
        if before and after:
            # A slice of redacted text is still redacted text.
            quote = RedactedText(match.group(0).strip())
            found.append(_Statement(artifact.id, quote, before, after))
    return found


def find_order_conflicts(artifacts: Sequence[Artifact]) -> tuple[Conflict, ...]:
    """Pairs of artifacts that state opposite orders. Each artifact is reported at most once."""
    statements = [
        statement
        for artifact in sorted(artifacts, key=lambda a: a.id.encode())
        for statement in _statements(artifact)
    ]
    conflicts: list[Conflict] = []
    used: set[str] = set()
    for index, one in enumerate(statements):
        for other in statements[index + 1 :]:
            if one.artifact_id == other.artifact_id or {one.artifact_id, other.artifact_id} & used:
                continue
            if one.before & other.after and one.after & other.before:
                conflicts.append(
                    Conflict(one.artifact_id, one.quote, other.artifact_id, other.quote)
                )
                used |= {one.artifact_id, other.artifact_id}
    return tuple(conflicts)
