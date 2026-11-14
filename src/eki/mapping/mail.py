"""Deterministic mail-thread and recurring-contact topics; never read fixture keys."""

import re
from collections import defaultdict

from eki.ingest.models import Artifact, Snapshot
from eki.mapping.models import TopicKind

Draft = tuple[TopicKind, str | None, str, list[str]]
IGNORED = frozenset(
    {
        "a",
        "an",
        "the",
        "and",
        "for",
        "of",
        "to",
        "in",
        "on",
        "with",
        "is",
        "at",
        "re",
        "fw",
        "fwd",
        "coming",
        "up",
        "about",
        "regarding",
        "update",
    }
)


def _terms(text: str) -> set[str]:
    return {
        w[:-1] if len(w) > 4 and w.endswith("s") else w
        for w in re.findall(r"[a-z0-9]+", text.casefold())
        if w not in IGNORED
    }


def mail_drafts(snapshot: Snapshot, artifacts: list[Artifact]) -> list[Draft]:
    threads: dict[str, list[Artifact]] = defaultdict(list)
    for artifact in artifacts:
        if artifact.kind == "email":
            threads[str(artifact.metadata.get("thread_id", artifact.id))].append(artifact)
    systems = sorted({a.system for a in artifacts if a.kind != "email" and a.system})
    output: list[Draft] = []
    contacts: dict[tuple[str | None, str], list[tuple[str, list[str]]]] = defaultdict(list)
    for thread_id, messages in sorted(threads.items()):
        label = re.sub(r"^(?:(?:re|fw|fwd):\s*)+", "", messages[0].title, flags=re.IGNORECASE)
        content = " ".join(a.title + " " + a.body for a in messages).casefold()
        counts = {
            s: len(
                re.findall(r"(?<![a-z0-9])" + re.escape(s.casefold()) + r"(?![a-z0-9])", content)
            )
            for s in systems
        }
        matching = [s for s in systems if counts[s]]
        system = min(matching, key=lambda s: (-counts[s], s)) if matching else None
        keywords = _terms(label) - _terms(system or "")
        linked = [
            a.id
            for a in artifacts
            if a.kind != "email"
            and a.system == system
            and keywords
            and len(_terms(a.title) & keywords) / len(keywords) >= 0.6
        ]
        ids = sorted({a.id for a in messages} | set(linked))
        authored = any(a.expert_role == "author" for a in messages)
        decision = authored and bool(
            _terms(content) & {"decision", "decided", "deliberate", "instead"}
        )
        output.append((TopicKind.DECISION if decision else TopicKind.PROCEDURE, system, label, ids))
        participants = {
            str(address).casefold()
            for a in messages
            for address in [
                a.metadata.get("sender", ""),
                *a.metadata.get("to", []),
                *a.metadata.get("cc", []),
            ]
            if isinstance(address, str)
            and "@" in address
            and address.casefold() != snapshot.expert_email.casefold()
        }
        for address in participants:
            contacts[(system, address)].append((thread_id, ids))
    for (system, address), groups in sorted(
        contacts.items(), key=lambda item: (item[0][0] or "", item[0][1])
    ):
        if len({thread for thread, _ in groups}) >= 2:
            ids = sorted({identifier for _, group in groups for identifier in group})
            output.append((TopicKind.CONTACT, system, f"{system or 'mail'} contact {address}", ids))
    return output
