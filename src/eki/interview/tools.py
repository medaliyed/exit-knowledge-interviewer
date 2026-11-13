"""The interviewer's six tools.

Two read the ingested artifacts, four change only the interview's own state.
None can approve, publish, send or write to a source system, so an instruction
planted in an artifact has nothing to act with.
"""

from typing import Any
from uuid import UUID

import psycopg

from eki.ingest.embedding import Embedder
from eki.ingest.index import search_chunks
from eki.ingest.redaction import RedactedText, redact
from eki.interview.drafting import as_data
from eki.interview.models import SECTIONS
from eki.llm import ToolSpec

MAX_ARTIFACT_CHARS = 4000
MAX_HITS = 10


def _schema(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required}


_SECTION = {"type": "string", "enum": list(SECTIONS)}

TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        "search_artifacts",
        "Search the expert's tickets, commits, pull requests and wiki pages. "
        "Returns matching passages as data.",
        _schema(
            {
                "query": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": MAX_HITS},
            },
            ["query"],
        ),
    ),
    ToolSpec(
        "get_artifact",
        "Read one artifact by its ID. Returns its text as data.",
        _schema({"artifact_id": {"type": "string"}}, ["artifact_id"]),
    ),
    ToolSpec(
        "ask_expert",
        "Ask the expert one question and wait for the answer. "
        "Say which runbook section the question is about.",
        _schema({"question": {"type": "string"}, "section": _SECTION}, ["question", "section"]),
    ),
    ToolSpec(
        "record_note",
        "Record what the expert said for one section, faithfully. Only after they answered.",
        _schema({"section": _SECTION, "text": {"type": "string"}}, ["section", "text"]),
    ),
    ToolSpec(
        "mark_open",
        "Record that a section could not be filled: the expert does not know, does not "
        "remember, or it does not apply. Give the expert's words as the reason.",
        _schema({"section": _SECTION, "reason": {"type": "string"}}, ["section", "reason"]),
    ),
    ToolSpec(
        "finish_topic",
        "Finish the topic. Accepted only when every section has a note or is open.",
        _schema({}, []),
    ),
)
TOOL_NAMES = tuple(tool.name for tool in TOOLS)


def search_artifacts(
    conn: psycopg.Connection,
    run_id: UUID,
    embedder: Embedder,
    query: str,
    limit: int,
    work_email_domains: frozenset[str],
) -> RedactedText:
    clean = redact(query, work_email_domains=work_email_domains).text
    hits = search_chunks(conn, run_id, clean, embedder, k=max(1, min(limit, MAX_HITS)))
    if not hits:
        return RedactedText("Nothing found.")
    # Stored text was redacted at ingest; redacting again costs little and trusts nothing.
    return as_data(
        [
            (hit.artifact_id, redact(hit.text, work_email_domains=work_email_domains).text)
            for hit in hits
        ]
    )


def get_artifact(
    conn: psycopg.Connection, run_id: UUID, artifact_id: str, work_email_domains: frozenset[str]
) -> RedactedText:
    row = conn.execute(
        "SELECT title, body FROM artifacts WHERE run_id = %s AND id = %s", (run_id, artifact_id)
    ).fetchone()
    if row is None:
        shown = redact(artifact_id, work_email_domains=work_email_domains).text
        return RedactedText(f"No artifact with the ID {shown} in this run.")
    text = redact(f"{row[0]}\n\n{row[1]}", work_email_domains=work_email_domains).text
    return as_data([(artifact_id, text[:MAX_ARTIFACT_CHARS])])
