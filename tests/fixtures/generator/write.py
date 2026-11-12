"""Turn the in-memory corpus into files, byte-for-byte reproducibly.

Rules that keep output identical across machines: keys are sorted, text is
UTF-8 with ``\\n`` line endings and a trailing newline, and nothing depends on
the clock, the locale, the environment or dictionary ordering.
"""

import json
import shutil
from pathlib import Path
from typing import Any

from tests.fixtures.generator.plants import SUMMARIES, TRAP_EXPECTED
from tests.fixtures.generator.world import (
    COLLEAGUES,
    DEPARTURE_DATE,
    EXPERT,
    FAKE_API_KEY,
    FAKE_PASSWORD,
    FAKE_TOKEN,
    HOME_ADDRESS,
    MINOR_SYSTEM,
    PHONE_NUMBER,
    SPAN_END,
    SPAN_START,
    SYSTEMS,
    VENDOR,
    Commit,
    Corpus,
    Email,
    PullRequest,
    Ticket,
    WikiPage,
    stamp,
)

GENERATOR_NAME = "eki-fixtures"
GENERATOR_VERSION = 1


class RefusingToOverwriteError(RuntimeError):
    """The target corpus directory holds something this generator did not write."""


def pretty(data: Any) -> str:
    return json.dumps(data, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def compact(data: Any) -> str:
    return json.dumps(data, sort_keys=True, ensure_ascii=False)


def ticket_record(ticket: Ticket) -> dict[str, Any]:
    return {
        "id": ticket.id,
        "system": ticket.system,
        "type": ticket.type,
        "title": ticket.title,
        "status": "open" if ticket.resolved_at is None else "closed",
        "priority": ticket.priority,
        "reporter": ticket.reporter.name,
        "assignee": ticket.assignee.name,
        "created_at": stamp(ticket.created_at),
        "resolved_at": None if ticket.resolved_at is None else stamp(ticket.resolved_at),
        "description": ticket.description,
        "labels": ticket.labels,
        "comments": [
            {"author": comment.author.name, "at": stamp(comment.at), "body": comment.body}
            for comment in ticket.comments
        ],
    }


def commit_record(commit: Commit) -> dict[str, Any]:
    return {
        "sha": commit.sha,
        "repo": commit.repo,
        "author_name": commit.author.name,
        "author_email": commit.author.email,
        "authored_at": stamp(commit.authored_at),
        "message": commit.message,
        "files": [
            {"path": f.path, "additions": f.additions, "deletions": f.deletions}
            for f in commit.files
        ],
        "patch": commit.patch,
        "pr_id": None if commit.pull_request is None else commit.pull_request.id,
    }


def pull_request_record(pull_request: PullRequest) -> dict[str, Any]:
    closed = pull_request.closed_at
    return {
        "id": pull_request.id,
        "repo": pull_request.repo,
        "title": pull_request.title,
        "author": pull_request.author.name,
        "state": pull_request.state,
        "created_at": stamp(pull_request.created_at),
        "closed_at": None if closed is None else stamp(closed),
        "description": pull_request.description,
        "commit_shas": [commit.sha for commit in pull_request.commits],
        "reviews": [
            {
                "reviewer": review.reviewer.name,
                "state": review.state,
                "at": stamp(review.at),
                "body": review.body,
            }
            for review in pull_request.reviews
        ],
    }


def email_record(email: Email, thread_ids: dict[str, str]) -> dict[str, Any]:
    return {
        "id": email.id,
        "folder": email.folder,
        "sensitivity": email.sensitivity,
        "from": email.sender,
        "to": email.to,
        "cc": email.cc,
        "sent_at": stamp(email.sent_at),
        "subject": email.subject,
        "body": email.body,
        "thread_id": thread_ids[email.thread],
    }


def wiki_text(page: WikiPage) -> str:
    authors = ", ".join(author.name for author in page.authors)
    front_matter = (
        f"---\nid: {page.id}\ntitle: {page.title}\n"
        f"last_updated: {page.last_updated.isoformat()}\nauthors: {authors}\n---\n"
    )
    return front_matter + page.body


def manifest(corpus: Corpus, seed: int) -> dict[str, Any]:
    people = [EXPERT, *COLLEAGUES, VENDOR]
    return {
        "generator": GENERATOR_NAME,
        "version": GENERATOR_VERSION,
        "seed": seed,
        "counts": {
            "tickets": len(corpus.tickets),
            "commits": len(corpus.commits),
            "pull_requests": len(corpus.pull_requests),
            "emails": len(corpus.emails),
            "wiki_pages": len(corpus.wiki),
        },
        "span": {"start": stamp(SPAN_START), "end": stamp(SPAN_END)},
        "expert": {
            "name": EXPERT.name,
            "email": EXPERT.email,
            "role": EXPERT.role,
            "departure_date": DEPARTURE_DATE.isoformat(),
        },
        "people": [{"name": p.name, "email": p.email, "role": p.role} for p in people],
        "systems": [*SYSTEMS, MINOR_SYSTEM],
    }


def answer_key(corpus: Corpus, seed: int) -> dict[str, Any]:
    tagged: dict[str, list[str]] = {plant: [] for plant in SUMMARIES}
    artifacts: list[tuple[str, set[str]]] = [
        *((t.id, t.plants) for t in corpus.tickets),
        *((c.sha, c.plants) for c in corpus.commits),
        *((p.id, p.plants) for p in corpus.pull_requests),
        *((e.id, e.plants) for e in corpus.emails),
        *((w.id, w.plants) for w in corpus.wiki),
    ]
    for artifact_id, plants in artifacts:
        for plant in plants:
            tagged[plant].append(artifact_id)

    def entry(plant: str) -> dict[str, Any]:
        result: dict[str, Any] = {
            "summary": SUMMARIES[plant],
            "artifact_ids": sorted(tagged[plant]),
        }
        if plant in TRAP_EXPECTED:
            result["expected"] = TRAP_EXPECTED[plant]
        return result

    return {
        "seed": seed,
        "generator_version": GENERATOR_VERSION,
        "gaps": {plant: entry(plant) for plant in SUMMARIES if plant.startswith("G")},
        "non_gaps": {plant: entry(plant) for plant in SUMMARIES if plant.startswith("N")},
        "traps": {plant: entry(plant) for plant in SUMMARIES if plant.startswith("T")},
        "never_index": [FAKE_API_KEY, FAKE_PASSWORD, FAKE_TOKEN, PHONE_NUMBER, HOME_ADDRESS],
        "excluded_mail_ids": sorted(e.id for e in corpus.emails if e.sensitivity != "normal"),
    }


def corpus_files(corpus: Corpus, seed: int) -> dict[str, str]:
    """Relative path under ``corpus/`` -> file content."""
    thread_ids: dict[str, str] = {}
    for email in corpus.emails:
        thread_ids.setdefault(email.thread, f"THR-{len(thread_ids) + 1:04d}")

    files = {"manifest.json": pretty(manifest(corpus, seed))}
    for ticket in corpus.tickets:
        files[f"tickets/{ticket.id}.json"] = pretty(ticket_record(ticket))
    for email in corpus.emails:
        files[f"mail/{email.folder}/{email.id}.json"] = pretty(email_record(email, thread_ids))
    for page in corpus.wiki:
        files[f"wiki/{page.slug}.md"] = wiki_text(page)
    files["git/commits.jsonl"] = "".join(
        compact(commit_record(commit)) + "\n" for commit in corpus.commits
    )
    files["git/pull_requests.jsonl"] = "".join(
        compact(pull_request_record(pull_request)) + "\n" for pull_request in corpus.pull_requests
    )
    return files


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content.encode("utf-8"))


def ensure_replaceable(corpus_dir: Path) -> None:
    """Refuse to touch a directory that is not empty and was not written by this generator."""
    if corpus_dir.is_symlink():
        raise RefusingToOverwriteError(f"{corpus_dir} is a symlink")
    if not corpus_dir.exists():
        return
    if not corpus_dir.is_dir():
        raise RefusingToOverwriteError(f"{corpus_dir} is not a directory")
    if not any(corpus_dir.iterdir()):
        return
    marker = corpus_dir / "manifest.json"
    try:
        written_by = json.loads(marker.read_text(encoding="utf-8")).get("generator")
    except (OSError, ValueError, AttributeError):
        written_by = None
    if written_by != GENERATOR_NAME:
        raise RefusingToOverwriteError(
            f"{corpus_dir} is not empty and has no {GENERATOR_NAME} manifest; not deleting it"
        )


def write_fixtures(out_dir: Path, corpus: Corpus, seed: int, answers: dict[str, Any]) -> None:
    corpus_dir = out_dir / "corpus"
    ensure_replaceable(corpus_dir)
    files = corpus_files(corpus, seed)
    if corpus_dir.exists():
        shutil.rmtree(corpus_dir)
    for relative, content in sorted(files.items()):
        _write(corpus_dir / relative, content)
    _write(out_dir / "answer_key.json", pretty(answer_key(corpus, seed)))
    _write(out_dir / "expert_answers.json", pretty(answers))
