"""F12: export complete approved entries, using the same live ACL gate as retrieval."""

import hashlib
import html
import re
from dataclasses import dataclass, field
from uuid import UUID

import psycopg

from eki.access import VISIBLE_RUN, approved_entry, visible_params
from eki.auth import Principal
from eki.errors import NotFoundError
from eki.ingest.redaction import redact
from eki.runbook.models import (
    SECTION_NAMES,
    ArtifactCitation,
    EntryStatus,
    RunbookEntry,
    validate_citations,
)


def render_markdown(
    entry: RunbookEntry, *, version: int, domains: frozenset[str] = frozenset(), level: int = 1
) -> str:
    entry = RunbookEntry.model_validate(entry.model_dump())
    if entry.status != EntryStatus.APPROVED or entry.missing_sections():
        raise ValueError("only complete approved entries can be exported")
    validate_citations(entry)

    def escape(text: str) -> str:
        clean = str(redact(text, work_email_domains=domains).text)
        clean = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", clean)
        clean = html.escape(clean, quote=False)
        return re.sub(r"([\\`*_{}\[\]()#+.!|\-])", r"\\\1", clean).replace("\n", "\n  ")

    prefix = hashlib.sha256(entry.id.encode()).hexdigest()[:12]
    sources: dict[str, tuple[str, str]] = {}
    lines = [
        f"{'#' * level} {escape(entry.title.replace(chr(10), ' '))}",
        "",
        f"Entry: {escape(entry.id)}  ",
        f"Version: {version}  ",
        f"Approved by: {escape(entry.approved_by or '')}  ",
        f"Approved at: {escape(entry.approved_at.isoformat() if entry.approved_at else '')}",
        "",
    ]
    for name in SECTION_NAMES:
        lines += [f"{'#' * (level + 1)} {name.title()}", ""]
        section = entry.section(name)
        for index, claim in enumerate(section.claims):
            references = []
            for citation in claim.citations:
                key = citation.model_dump_json()
                if key not in sources:
                    label = f"source-{prefix}-{len(sources) + 1}"
                    if isinstance(citation, ArtifactCitation):
                        description = f"Artifact {escape(citation.artifact_id)}"
                        if citation.quote:
                            description += ": " + escape(citation.quote)
                    else:
                        description = (
                            f"Interview {escape(citation.session_id)}, turn {citation.turn_index}"
                        )
                    sources[key] = label, description
                references.append(f"[^{sources[key][0]}]")
            marker = f"{index + 1}." if name == "steps" else "-"
            lines.append(f"{marker} {escape(claim.text)} {''.join(references)}")
        if section.open_question is not None:
            lines.append("**Open question:** " + escape(section.open_question))
        lines.append("")
    lines += [f"[^{label}]: {description}" for label, description in sources.values()]
    return "\n".join(lines).rstrip() + "\n"


@dataclass
class ExportService:
    dsn: str
    work_email_domains: frozenset[str] = field(default_factory=frozenset)

    def entry(self, principal: Principal, entry_id: UUID) -> str:
        with psycopg.connect(self.dsn, autocommit=True) as conn:
            stored = approved_entry(conn, entry_id, principal)
            return render_markdown(
                stored.entry, version=stored.version, domains=self.work_email_domains
            )

    def run(self, principal: Principal, run_id: UUID) -> str:
        with psycopg.connect(self.dsn, autocommit=True) as conn:
            allowed = conn.execute(
                "SELECT 1 FROM ingest_runs r WHERE r.id = %s AND " + VISIBLE_RUN,  # noqa: S608
                (run_id, *visible_params(principal)),
            ).fetchone()
            if allowed is None:
                raise NotFoundError("run not found")
            ids = conn.execute(
                "SELECT e.id FROM runbook_entries e JOIN sessions s ON s.id = e.session_id "
                "JOIN interview_plans p ON p.id = s.plan_id WHERE p.run_id = %s "
                "AND e.status = 'approved' ORDER BY e.title, e.id",
                (run_id,),
            ).fetchall()
            documents = []
            for (entry_id,) in ids:
                stored = approved_entry(conn, entry_id, principal)
                documents.append(
                    render_markdown(
                        stored.entry,
                        version=stored.version,
                        domains=self.work_email_domains,
                        level=2,
                    )
                )
        return "# Approved runbook\n\n" + (
            "\n".join(documents) if documents else "No approved entries.\n"
        )
