"""Read-only access to a source directory, fixed to one expert and one date range.

The scope is decided when a server starts. No tool takes an expert or a date as an
argument, so a caller cannot widen it. The reader opens only the sub-directories a
server was given and refuses everything else, including mail and the answer key.
"""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from eki.ingest.redaction import KINDS, redact
from eki.ingest.scope import Scope

__all__ = ["Redactor", "Scope", "SourceAccessError", "SourceReader", "parse_time"]


class SourceAccessError(PermissionError):
    """Raised for any attempt to read outside the directories a server may open."""


def parse_time(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


class SourceReader:
    def __init__(self, source_dir: Path, allowed: tuple[str, ...]) -> None:
        self._root = source_dir.resolve()
        self._allowed = tuple((self._root / name).resolve() for name in allowed)

    def resolve(self, relative: str) -> Path:
        path = (self._root / relative).resolve()
        if not any(path.is_relative_to(directory) for directory in self._allowed):
            raise SourceAccessError(f"{relative!r} is outside the directories this server may read")
        return path

    def read_text(self, relative: str) -> str:
        return self.resolve(relative).read_text(encoding="utf-8")

    def read_json(self, relative: str) -> dict[str, Any]:
        loaded: dict[str, Any] = json.loads(self.read_text(relative))
        return loaded

    def read_jsonl(self, relative: str) -> list[dict[str, Any]]:
        return [json.loads(line) for line in self.read_text(relative).split("\n") if line]

    def list_files(self, directory: str, pattern: str) -> list[str]:
        base = self.resolve(directory)
        return sorted(path.relative_to(self._root).as_posix() for path in base.glob(pattern))


class Redactor:
    """Redacts the text fields of one item and totals what it removed."""

    def __init__(self, work_email_domains: frozenset[str]) -> None:
        self._domains = work_email_domains
        self.counts = dict.fromkeys(KINDS, 0)

    def __call__(self, text: str) -> str:
        result = redact(text, work_email_domains=self._domains)
        for kind, count in result.counts.items():
            self.counts[kind] += count
        return result.text

    def optional(self, text: str | None) -> str | None:
        return None if text is None else self(text)
