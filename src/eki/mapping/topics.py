"""Group artifacts into topics (F4). Pure, deterministic, no model.

Rules, in the order they are applied. An artifact may belong to its system topic and
to one more specific topic. Documents never belong to a topic: they are linked to
topics when gaps are scored.

* system: one per repo or ticket system that has at least one artifact of the expert.
* incident: tickets of type ``incident`` in one system, grouped by title similarity.
* procedure (tickets): tickets of type ``task`` or ``request``, grouped the same way.
* procedure (commits): the expert's single-path commits, grouped by (repo, path) and then
  by message similarity with digits ignored; a cluster of 3 or more is a procedure.
* decision: each pull request whose review by the expert was ``rejected`` or
  ``changes_requested``.

Similarity is cosine similarity of embeddings, linked transitively (single link).
"""

import re
from collections import Counter, defaultdict

from eki.ingest.embedding import Embedder
from eki.ingest.models import Artifact, Snapshot
from eki.ingest.redaction import RedactedText
from eki.mapping.models import Topic, TopicKind

TICKET_SIMILARITY = 0.75
COMMIT_SIMILARITY = 0.7
MIN_TICKET_GROUP = 2
MIN_COMMIT_GROUP = 3
BLOCKING_REVIEW_STATES = frozenset({"rejected", "changes_requested"})

DIGITS = re.compile(r"\d+")


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "topic"


def _cluster(texts: list[str], embedder: Embedder, threshold: float) -> list[list[int]]:
    """Single-link clusters of the indexes of ``texts``, each sorted, ordered by first member."""
    vectors = embedder.embed([RedactedText(t) for t in texts])
    parent = list(range(len(texts)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(texts)):
        for j in range(i + 1, len(texts)):
            if sum(a * b for a, b in zip(vectors[i], vectors[j], strict=True)) >= threshold:
                parent[find(j)] = find(i)
    groups: dict[int, list[int]] = defaultdict(list)
    for i in range(len(texts)):
        groups[find(i)].append(i)
    return sorted(groups.values(), key=lambda members: members[0])


def _most_common(values: list[str]) -> str:
    counts = Counter(values)
    best = max(counts.values())
    return min(value for value, count in counts.items() if count == best)


def _unique_id(base: str, taken: set[str]) -> str:
    candidate, number = base, 2
    while candidate in taken:
        candidate, number = f"{base}-{number}", number + 1
    taken.add(candidate)
    return candidate


def _first_line(text: str) -> str:
    return text.split("\n", 1)[0]


def build_topics(snapshot: Snapshot, embedder: Embedder) -> tuple[Topic, ...]:
    """Topics for one snapshot, sorted by ID. The same input always gives the same output."""
    artifacts = sorted(
        (a for a in snapshot.artifacts if a.kind != "document"),
        key=lambda a: (a.occurred_at, a.id),
    )
    drafts: list[tuple[TopicKind, str | None, str, list[str]]] = []

    by_system: dict[str, list[Artifact]] = defaultdict(list)
    for artifact in artifacts:
        if artifact.system is not None:
            by_system[artifact.system].append(artifact)
    for system in sorted(by_system):
        drafts.append((TopicKind.SYSTEM, system, system, [a.id for a in by_system[system]]))

    tickets = [a for a in artifacts if a.kind == "ticket" and a.system is not None]
    for kind, is_incident in ((TopicKind.INCIDENT, True), (TopicKind.PROCEDURE, False)):
        for system in sorted({t.system for t in tickets if t.system}):
            pool = [
                t
                for t in tickets
                if t.system == system and (t.metadata["type"] == "incident") == is_incident
            ]
            if not pool:
                continue
            for members in _cluster([t.title for t in pool], embedder, TICKET_SIMILARITY):
                if len(members) >= MIN_TICKET_GROUP:
                    group = [pool[i] for i in members]
                    label = _most_common([t.title for t in group])
                    drafts.append((kind, system, label, [t.id for t in group]))

    singles: dict[tuple[str, str], list[Artifact]] = defaultdict(list)
    for artifact in artifacts:
        if artifact.kind == "commit" and len(artifact.metadata["files"]) == 1:
            singles[(artifact.metadata["repo"], artifact.metadata["files"][0]["path"])].append(
                artifact
            )
    for repo, path in sorted(singles):
        group_commits = singles[(repo, path)]
        normalised = [DIGITS.sub("#", _first_line(c.title)) for c in group_commits]
        for members in _cluster(normalised, embedder, COMMIT_SIMILARITY):
            if len(members) >= MIN_COMMIT_GROUP:
                chosen = [group_commits[i] for i in members]
                message = _most_common([_first_line(c.title) for c in chosen])
                drafts.append(
                    (TopicKind.PROCEDURE, repo, f"{path}: {message}", [c.id for c in chosen])
                )

    for artifact in artifacts:
        if artifact.kind != "pull_request":
            continue
        mine = [r for r in artifact.metadata["reviews"] if r["reviewer"] == snapshot.expert_name]
        if any(r["state"] in BLOCKING_REVIEW_STATES for r in mine):
            drafts.append((TopicKind.DECISION, artifact.system, artifact.title, [artifact.id]))

    taken: set[str] = set()
    topics = [
        Topic(
            id=_unique_id(f"{kind.value}:{system or '-'}:{slug(label)}", taken),
            kind=kind,
            system=system,
            label=label,
            artifact_ids=tuple(sorted(ids, key=lambda i: i.encode())),
        )
        for kind, system, label, ids in drafts
    ]
    return tuple(sorted(topics, key=lambda t: t.id))
