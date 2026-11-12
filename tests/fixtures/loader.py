"""Read a generated corpus back from disk, for tests only.

Nothing here knows how the corpus was built. It reads what a pipeline would
read, plus the answer key and scripted expert answers that only tests may see.
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

Record = dict[str, Any]


@dataclass(frozen=True)
class WikiPage:
    id: str
    slug: str
    meta: dict[str, str]
    body: str


@dataclass(frozen=True)
class LoadedCorpus:
    root: Path
    manifest: Record
    tickets: list[Record]
    commits: list[Record]
    pull_requests: list[Record]
    emails: list[Record]
    wiki: list[WikiPage]
    texts: dict[str, str]
    """Artifact ID -> the raw text a pipeline would see for that artifact."""
    answer_key: Record
    expert_answers: Record


def _read_json(path: Path) -> Record:
    loaded: Record = json.loads(path.read_text(encoding="utf-8"))
    return loaded


def _read_jsonl(path: Path) -> list[tuple[Record, str]]:
    lines = [line for line in path.read_text(encoding="utf-8").split("\n") if line]
    return [(json.loads(line), line) for line in lines]


def _read_wiki(path: Path) -> WikiPage:
    text = path.read_text(encoding="utf-8")
    _, front_matter, body = text.split("---\n", 2)
    meta = dict(line.split(": ", 1) for line in front_matter.splitlines() if line)
    return WikiPage(id=meta["id"], slug=path.stem, meta=meta, body=body)


def load_corpus(fixtures_dir: Path) -> LoadedCorpus:
    corpus = fixtures_dir / "corpus"
    texts: dict[str, str] = {}

    tickets = []
    for path in sorted((corpus / "tickets").glob("*.json")):
        tickets.append(_read_json(path))
        texts[path.stem] = path.read_text(encoding="utf-8")

    emails = []
    for path in sorted((corpus / "mail").glob("*/*.json")):
        emails.append(_read_json(path))
        texts[path.stem] = path.read_text(encoding="utf-8")

    commits = []
    for record, line in _read_jsonl(corpus / "git" / "commits.jsonl"):
        commits.append(record)
        texts[record["sha"]] = line

    pull_requests = []
    for record, line in _read_jsonl(corpus / "git" / "pull_requests.jsonl"):
        pull_requests.append(record)
        texts[record["id"]] = line

    wiki = []
    for path in sorted((corpus / "wiki").glob("*.md")):
        page = _read_wiki(path)
        wiki.append(page)
        texts[page.id] = path.read_text(encoding="utf-8")

    return LoadedCorpus(
        root=fixtures_dir,
        manifest=_read_json(corpus / "manifest.json"),
        tickets=tickets,
        commits=commits,
        pull_requests=pull_requests,
        emails=emails,
        wiki=wiki,
        texts=texts,
        answer_key=_read_json(fixtures_dir / "answer_key.json"),
        expert_answers=_read_json(fixtures_dir / "expert_answers.json"),
    )
