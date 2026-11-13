"""Pull an expert's artifacts through the MCP servers and keep a redacted snapshot.

The servers redact before anything leaves them. This client treats their output as
untrusted anyway: it validates every shape and runs every text field through
``redact`` again, so ``Artifact`` can only ever hold ``RedactedText``. Logging here
carries IDs and counts only, never content.
"""

import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import psycopg
from mcp import Client, StdioServerParameters
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ValidationError

from eki.ingest import wire
from eki.ingest.documents import paths_mentioned
from eki.ingest.models import ActivitySummary, Artifact, Snapshot
from eki.ingest.redaction import KINDS, RedactedText, redact
from mcp_servers.common.source import Scope

log = logging.getLogger("eki.ingest")

REPO_ROOT = Path(__file__).resolve().parents[3]
PAGE_SIZE = 100


class IngestError(RuntimeError):
    """A server failed, or sent something that does not match the expected shape."""


def _first_ingest_error(error: BaseException) -> IngestError | None:
    if isinstance(error, IngestError):
        return error
    if isinstance(error, BaseExceptionGroup):
        for inner in error.exceptions:
            found = _first_ingest_error(inner)
            if found is not None:
                return found
    return None


@contextmanager
def ingest_errors() -> Iterator[None]:
    """Surface an ``IngestError`` that the MCP client wrapped in (nested) exception groups."""
    try:
        yield
    except BaseExceptionGroup as group:
        found = _first_ingest_error(group)
        if found is not None:
            raise found from None
        raise


def _time(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def _optional_time(text: str | None) -> datetime | None:
    return None if text is None else _time(text)


class _Cleaner:
    """Redacts text for one artifact and totals what was removed."""

    def __init__(self, domains: frozenset[str]) -> None:
        self._domains = domains
        self.counts = dict.fromkeys(KINDS, 0)

    def __call__(self, text: str) -> RedactedText:
        result = redact(text, work_email_domains=self._domains)
        for kind, count in result.counts.items():
            self.counts[kind] += count
        return result.text


async def _call(client: Client, tool: str, arguments: dict[str, Any] | None = None) -> Any:
    try:
        result = await client.call_tool(tool, arguments or {})
    except Exception as error:
        raise IngestError(f"{tool} failed: {type(error).__name__}") from error
    if result.is_error or result.structured_content is None:
        raise IngestError(f"{tool} returned an error")
    return result.structured_content


def _validate[T: BaseModel](tool: str, model: type[T], data: Any) -> T:
    try:
        return model.model_validate(data)
    except ValidationError as error:
        raise IngestError(
            f"{tool} returned an unexpected shape ({error.error_count()} errors)"
        ) from error


async def _all_pages[T: BaseModel](client: Client, tool: str, page_model: type[T]) -> list[Any]:
    items: list[Any] = []
    offset: int | None = 0
    while offset is not None:
        data = await _call(client, tool, {"offset": offset, "limit": PAGE_SIZE})
        page = _validate(tool, page_model, data)
        items.extend(page.items)  # type: ignore[attr-defined]
        offset = page.next_offset  # type: ignore[attr-defined]
    return items


async def _list[T: BaseModel](
    client: Client, tool: str, item_model: type[T], arguments: dict[str, Any] | None = None
) -> list[T]:
    data = await _call(client, tool, arguments)
    rows = data.get("result") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise IngestError(f"{tool} returned an unexpected shape")
    return [_validate(tool, item_model, row) for row in rows]


def _commit_artifact(raw: wire.WireCommit, domains: frozenset[str]) -> Artifact:
    clean = _Cleaner(domains)
    message = clean(raw.message)
    patch = clean(raw.patch) if raw.patch else None
    body = message if patch is None else f"{message}\n\n{patch}"
    return Artifact(
        id=raw.sha,
        source="git",
        kind="commit",
        system=raw.repo,
        occurred_at=_time(raw.authored_at),
        title=RedactedText(message.split("\n", 1)[0]),
        body=RedactedText(body),
        expert_role="author",
        metadata={
            "repo": raw.repo,
            "pr_id": raw.pr_id,
            "files": [
                {"path": clean(f.path), "additions": f.additions, "deletions": f.deletions}
                for f in raw.files
            ],
        },
        redaction_counts=clean.counts,
    )


def _pull_request_artifact(raw: wire.WirePullRequest, domains: frozenset[str]) -> Artifact:
    clean = _Cleaner(domains)
    reviews = [
        {"reviewer": r.reviewer, "state": r.state, "at": r.at, "body": clean(r.body)}
        for r in raw.reviews
    ]
    review_text = "\n".join(
        f"Review by {r['reviewer']} ({r['state']}): {r['body']}" for r in reviews
    )
    description = clean(raw.description)
    return Artifact(
        id=raw.id,
        source="git",
        kind="pull_request",
        system=raw.repo,
        occurred_at=_time(raw.created_at),
        title=clean(raw.title),
        body=RedactedText(f"{description}\n\n{review_text}".strip()),
        expert_role=raw.expert_role,
        metadata={
            "repo": raw.repo,
            "author": raw.author,
            "state": raw.state,
            "closed_at": raw.closed_at,
            "commit_shas": raw.commit_shas,
            "reviews": reviews,
        },
        redaction_counts=clean.counts,
    )


def _ticket_artifact(raw: wire.WireTicket, domains: frozenset[str]) -> Artifact:
    clean = _Cleaner(domains)
    comments = [{"author": c.author, "at": c.at, "body": clean(c.body)} for c in raw.comments]
    thread = "\n".join(f"{c['author']}: {c['body']}" for c in comments)
    description = clean(raw.description)
    return Artifact(
        id=raw.id,
        source="tickets",
        kind="ticket",
        system=raw.system,
        occurred_at=_time(raw.created_at),
        title=clean(raw.title),
        body=RedactedText(f"{description}\n\n{thread}".strip()),
        expert_role=raw.expert_role,
        metadata={
            "type": raw.type,
            "priority": raw.priority,
            "status": raw.status,
            "assignee": raw.assignee,
            "reporter": raw.reporter,
            "resolved_at": raw.resolved_at,
            "labels": raw.labels,
            "comments": comments,
        },
        redaction_counts=clean.counts,
    )


def _document_artifact(raw: wire.WireDocument, domains: frozenset[str]) -> Artifact:
    clean = _Cleaner(domains)
    updated = date.fromisoformat(raw.last_updated)
    return Artifact(
        id=raw.id,
        source="git",
        kind="document",
        system=None,
        occurred_at=datetime(updated.year, updated.month, updated.day, tzinfo=UTC),
        title=clean(raw.title),
        body=clean(raw.body),
        expert_role="none",
        metadata={"slug": raw.slug, "last_updated": raw.last_updated, "authors": raw.authors},
        redaction_counts=clean.counts,
    )


def mentioned_paths(documents: list[wire.WireDocument]) -> set[str]:
    """File paths any wiki page mentions in backticks, such as `deploy/cron.yaml`."""
    return {path for document in documents for path in paths_mentioned(document.body)}


async def pull(git: Client, tickets: Client, scope: Scope) -> Snapshot:
    """Read everything in scope through the two MCP clients. Touches no database."""
    domains = scope.work_email_domains
    commits = await _all_pages(git, "list_commits", wire.WireCommitPage)
    pull_requests = await _all_pages(git, "list_pull_requests", wire.WirePullRequestPage)
    documents = await _list(git, "list_documents", wire.WireDocument)
    repos = await _list(git, "get_repo_activity", wire.WireRepoActivity)
    ticket_items = await _all_pages(tickets, "list_tickets", wire.WireTicketPage)
    groups = await _list(tickets, "get_ticket_activity", wire.WireTicketGroup)

    wanted_paths = mentioned_paths(documents)
    paths: list[wire.WirePathActivity] = []
    for repo in sorted({r.repo for r in repos}):
        touched = {f.path for c in commits if c.repo == repo for f in c.files}
        asked = sorted(touched | wanted_paths)
        if asked:
            paths += await _list(
                git, "get_path_activity", wire.WirePathActivity, {"repo": repo, "paths": asked}
            )

    artifacts = [
        *(_commit_artifact(c, domains) for c in commits),
        *(_pull_request_artifact(p, domains) for p in pull_requests),
        *(_ticket_artifact(t, domains) for t in ticket_items),
        *(_document_artifact(d, domains) for d in documents),
    ]
    artifacts.sort(key=lambda a: (a.occurred_at, a.id))

    clean = _Cleaner(domains)
    activity = [
        *(
            ActivitySummary(
                scope="repo",
                system=r.repo,
                key=r.repo,
                total=r.total_commits,
                by_expert=r.expert_commits,
                other_actors=r.other_authors,
                last_at=_optional_time(r.last_commit_at),
            )
            for r in repos
        ),
        *(
            ActivitySummary(
                scope="path",
                system=p.repo,
                key=p.path,
                total=p.total_commits,
                by_expert=p.expert_commits,
                other_actors=p.other_authors,
                last_at=_optional_time(p.last_changed_at),
            )
            for p in paths
        ),
        *(
            ActivitySummary(
                scope="ticket_group",
                system=g.system,
                key=f"{g.type}|{clean(g.title)}",
                total=g.total_tickets,
                by_expert=g.expert_tickets,
                other_actors=g.other_assignees,
                last_at=_optional_time(g.last_created_at),
            )
            for g in groups
        ),
    ]
    activity.sort(key=lambda a: (a.scope, a.system, a.key))

    log.info(
        "pulled %d commits, %d pull requests, %d tickets, %d documents, %d activity rows",
        len(commits),
        len(pull_requests),
        len(ticket_items),
        len(documents),
        len(activity),
    )
    return Snapshot(
        expert_name=scope.expert_name,
        expert_email=scope.expert_email,
        since=scope.since,
        until=scope.until,
        artifacts=tuple(artifacts),
        activity=tuple(activity),
    )


def server_parameters(module: str, scope: Scope, source_dir: Path) -> StdioServerParameters:
    """How to start one MCP server as a subprocess for this scope."""
    arguments = [
        "-m",
        module,
        "--source-dir",
        str(source_dir),
        "--expert-name",
        scope.expert_name,
        "--expert-email",
        scope.expert_email,
        "--since",
        scope.since.date().isoformat(),
        "--until",
        scope.until.date().isoformat(),
    ]
    for domain in sorted(scope.work_email_domains):
        arguments += ["--work-domain", domain]
    return StdioServerParameters(command=sys.executable, args=arguments, cwd=str(REPO_ROOT))


async def ingest_from_source(scope: Scope, source_dir: Path) -> Snapshot:
    """Start both servers as subprocesses over stdio and pull a snapshot."""
    with ingest_errors():
        async with (
            Client(server_parameters("mcp_servers.git_server", scope, source_dir)) as git,
            Client(server_parameters("mcp_servers.tickets_server", scope, source_dir)) as tickets,
        ):
            return await pull(git, tickets, scope)


def store(conn: psycopg.Connection, snapshot: Snapshot) -> UUID:
    """Write a snapshot as a new run, atomically. Returns the run ID."""
    run_id = uuid4()
    try:
        with conn.transaction():
            conn.execute(
                "INSERT INTO ingest_runs (id, expert_name, expert_email, since, until, status) "
                "VALUES (%s, %s, %s, %s, %s, 'running')",
                (
                    run_id,
                    snapshot.expert_name,
                    snapshot.expert_email,
                    snapshot.since,
                    snapshot.until,
                ),
            )
            with conn.cursor() as cursor:
                cursor.executemany(
                    "INSERT INTO artifacts (run_id, id, source, kind, system, occurred_at, title, "
                    "body, expert_role, metadata, redaction_counts) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    [
                        (
                            run_id,
                            a.id,
                            a.source,
                            a.kind,
                            a.system,
                            a.occurred_at,
                            a.title,
                            a.body,
                            a.expert_role,
                            Jsonb(a.metadata),
                            Jsonb(a.redaction_counts),
                        )
                        for a in snapshot.artifacts
                    ],
                )
                cursor.executemany(
                    "INSERT INTO activity (run_id, scope, system, key, total, by_expert, "
                    "other_actors, last_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                    [
                        (
                            run_id,
                            s.scope,
                            s.system,
                            s.key,
                            s.total,
                            s.by_expert,
                            s.other_actors,
                            s.last_at,
                        )
                        for s in snapshot.activity
                    ],
                )
            conn.execute(
                "UPDATE ingest_runs SET status = 'completed', finished_at = now() WHERE id = %s",
                (run_id,),
            )
    except psycopg.Error as error:
        raise IngestError(f"could not store the snapshot: {type(error).__name__}") from error
    log.info("stored run %s with %d artifacts", run_id, len(snapshot.artifacts))
    return run_id


def load(conn: psycopg.Connection, run_id: UUID) -> Snapshot:
    """Read a stored run back. Raises ``IngestError`` for an unknown run."""
    run = conn.execute(
        "SELECT expert_name, expert_email, since, until FROM ingest_runs "
        "WHERE id = %s AND status = 'completed'",
        (run_id,),
    ).fetchone()
    if run is None:
        raise IngestError(f"no completed run {run_id}")
    artifact_rows = conn.execute(
        "SELECT id, source, kind, system, occurred_at, title, body, expert_role, metadata, "
        'redaction_counts FROM artifacts WHERE run_id = %s ORDER BY occurred_at, id COLLATE "C"',
        (run_id,),
    ).fetchall()
    activity_rows = conn.execute(
        "SELECT scope, system, key, total, by_expert, other_actors, last_at FROM activity "
        'WHERE run_id = %s ORDER BY scope COLLATE "C", system COLLATE "C", key COLLATE "C"',
        (run_id,),
    ).fetchall()
    return Snapshot(
        expert_name=run[0],
        expert_email=run[1],
        since=run[2],
        until=run[3],
        artifacts=tuple(
            Artifact(
                id=r[0],
                source=r[1],
                kind=r[2],
                system=r[3],
                occurred_at=r[4],
                title=RedactedText(r[5]),
                body=RedactedText(r[6]),
                expert_role=r[7],
                metadata=r[8],
                redaction_counts=r[9],
            )
            for r in artifact_rows
        ),
        activity=tuple(
            ActivitySummary(
                scope=r[0],
                system=r[1],
                key=r[2],
                total=r[3],
                by_expert=r[4],
                other_actors=r[5],
                last_at=r[6],
            )
            for r in activity_rows
        ),
    )
