"""The gap score (F5): how likely a topic is knowledge that exists only in the expert's head.

    score = 100 x exclusivity x recency x (0.5 x doc_gap + 0.5 x criticality)

Every component is between 0 and 1 and comes with a plain sentence saying which numbers
it was computed from. The formula and the weights were fixed before any code ran and are
not tuned against the answer key.

* exclusivity: the expert's share of the work, from the activity counts (never from content).
* doc_gap: 1.0 with no linked wiki page, 0.5 if every linked page is out of date, 0.0 otherwise.
* recency: 0.5 ** (months since her last artifact in the topic / 12).
* criticality: the highest ticket priority in the topic (P1 1.0, P2 0.75, P3 0.5, P4 0.25),
  or 0.5 when the topic has no tickets.

A page is linked to a topic when it names the topic's system in its title and at least 60% of
the rest of its title words occur in the topic's label. A system topic links to every page of
its system. A page is out of date when a file it mentions changed after the page's date.
"""

import re
from datetime import date, datetime

from eki.ingest.documents import paths_mentioned
from eki.ingest.models import ActivitySummary, Artifact, Snapshot
from eki.mapping.models import DocStatus, GapScore, Topic, TopicKind

PRIORITY_WEIGHT = {"P1": 1.0, "P2": 0.75, "P3": 0.5, "P4": 0.25}
DEFAULT_CRITICALITY = 0.5
LINK_THRESHOLD = 0.6
DAYS_PER_MONTH = 30.4375
STOP_WORDS = frozenset(
    {"the", "a", "an", "and", "for", "of", "to", "in", "on", "with", "is", "at"}
    | {"by", "from", "or", "after", "during"}
)
WORD = re.compile(r"[a-z0-9]+")


def _words(text: str) -> set[str]:
    return set(WORD.findall(text.lower()))


def _share(by_expert: int, total: int) -> float:
    return by_expert / total if total else 0.0


def _day(moment: datetime | date) -> date:
    return moment.date() if isinstance(moment, datetime) else moment


class _Context:
    def __init__(self, snapshot: Snapshot) -> None:
        self.snapshot = snapshot
        self.artifacts = {a.id: a for a in snapshot.artifacts}
        self.documents = [a for a in snapshot.artifacts if a.kind == "document"]
        self.repos: dict[str, ActivitySummary] = {}
        self.paths: dict[tuple[str, str], ActivitySummary] = {}
        self.groups: dict[tuple[str, str], ActivitySummary] = {}
        self.latest_change: dict[str, datetime] = {}
        for row in snapshot.activity:
            if row.scope == "repo":
                self.repos[row.system] = row
            elif row.scope == "path":
                self.paths[(row.system, row.key)] = row
                if row.last_at is not None and row.last_at > self.latest_change.get(
                    row.key, datetime.min.replace(tzinfo=row.last_at.tzinfo)
                ):
                    self.latest_change[row.key] = row.last_at
            else:
                self.groups[(row.system, row.key)] = row
        names = {row.system for row in snapshot.activity} | {
            a.system for a in snapshot.artifacts if a.system
        }
        self.systems = sorted(names)

    def page_systems(self, page: Artifact) -> set[str]:
        title = page.title.lower()
        return {system for system in self.systems if system.lower() in title}

    def stale_reason(self, page: Artifact) -> str | None:
        """The first file the page mentions that changed after the page was last updated."""
        updated = date.fromisoformat(page.metadata["last_updated"])
        for path in sorted(paths_mentioned(page.body)):
            changed = self.latest_change.get(path)
            if changed is not None and _day(changed) > updated:
                return (
                    f"`{path}` changed on {_day(changed).isoformat()}, after the page "
                    f"{page.id} was last updated on {updated.isoformat()}"
                )
        return None


def _link_pages(topic: Topic, context: _Context) -> list[Artifact]:
    if topic.system is None:
        return []
    linked = []
    for page in context.documents:
        if topic.system not in context.page_systems(page):
            continue
        if topic.kind is TopicKind.SYSTEM:
            linked.append(page)
            continue
        system_words = _words(topic.system)
        wanted = _words(page.title) - STOP_WORDS - system_words
        if wanted and len(wanted & _words(topic.label)) / len(wanted) >= LINK_THRESHOLD:
            linked.append(page)
    return sorted(linked, key=lambda p: p.id)


def _exclusivity(topic: Topic, members: list[Artifact], context: _Context) -> tuple[float, str]:
    if topic.kind is TopicKind.DECISION:
        return 1.0, "The expert made this decision herself, so her share is counted as 100%."
    if topic.kind is TopicKind.SYSTEM and topic.system is not None:
        repo = context.repos.get(topic.system)
        if repo is not None:
            return (
                _share(repo.by_expert, repo.total),
                f"She made {repo.by_expert} of {repo.total} commits to {topic.system} "
                f"({_share(repo.by_expert, repo.total):.0%}).",
            )
        rows = [row for (system, _), row in context.groups.items() if system == topic.system]
        if rows:
            mine, total = sum(r.by_expert for r in rows), sum(r.total for r in rows)
            return _share(
                mine, total
            ), f"She was assigned {mine} of {total} {topic.system} tickets."
    tickets = [a for a in members if a.kind == "ticket"]
    if tickets and topic.system is not None:
        keys = sorted({f"{t.metadata['type']}|{t.title}" for t in tickets})
        rows = [
            context.groups[(topic.system, key)]
            for key in keys
            if (topic.system, key) in context.groups
        ]
        if rows:
            mine, total = sum(r.by_expert for r in rows), sum(r.total for r in rows)
            return (
                _share(mine, total),
                f"She was assigned {mine} of {total} tickets with these titles "
                f"({_share(mine, total):.0%}).",
            )
    commits = [a for a in members if a.kind == "commit"]
    if commits and topic.system is not None:
        paths = {c.metadata["files"][0]["path"] for c in commits if len(c.metadata["files"]) == 1}
        rows = [
            context.paths[(topic.system, p)]
            for p in sorted(paths)
            if (topic.system, p) in context.paths
        ]
        if rows:
            mine, total = sum(r.by_expert for r in rows), sum(r.total for r in rows)
            return (
                _share(mine, total),
                f"She made {mine} of {total} commits to {', '.join(sorted(paths))} "
                f"({_share(mine, total):.0%}).",
            )
    if not members:
        return 0.0, "None of this topic's artifacts are in the snapshot, so no share can be shown."
    return 1.0, (
        "No activity data for this topic (no activity data means only her own artifacts "
        "were seen), so her share is counted as 100%."
    )


def _doc_gap(topic: Topic, context: _Context) -> tuple[float, DocStatus, list[Artifact], str]:
    pages = _link_pages(topic, context)
    if not pages:
        return 1.0, DocStatus.NONE, [], "No wiki page covers this topic."
    reasons = [context.stale_reason(page) for page in pages]
    if all(reason is not None for reason in reasons):
        return (
            0.5,
            DocStatus.PARTIAL,
            pages,
            f"{len(pages)} linked page(s), all out of date: {reasons[0]}.",
        )
    names = ", ".join(page.id for page in pages)
    return 0.0, DocStatus.DOCUMENTED, pages, f"Documented by {names}, which is up to date."


def _recency(members: list[Artifact], as_of: datetime) -> tuple[float, str]:
    if not members:
        return 0.0, "No artifacts in the snapshot."
    last = max(a.occurred_at for a in members)
    months = max((as_of - last).days / DAYS_PER_MONTH, 0.0)
    factor = 0.5 ** (months / 12)
    return factor, (
        f"Her last activity was {last.date().isoformat()}, {months:.1f} months before the end "
        f"of the range (factor {factor:.2f})."
    )


def _criticality(members: list[Artifact]) -> tuple[float, str]:
    priorities = [a.metadata["priority"] for a in members if a.kind == "ticket"]
    known = [p for p in priorities if p in PRIORITY_WEIGHT]
    if not known:
        return DEFAULT_CRITICALITY, "No tickets, so criticality is the middle value 0.50."
    top = min(known)
    return PRIORITY_WEIGHT[
        top
    ], f"The highest ticket priority is {top} ({PRIORITY_WEIGHT[top]:.2f})."


def score_topics(
    topics: tuple[Topic, ...], snapshot: Snapshot, *, as_of: datetime
) -> tuple[GapScore, ...]:
    """Score every topic. Pure. Sorted by score, highest first, then by topic ID."""
    context = _Context(snapshot)
    scored: list[GapScore] = []
    for topic in topics:
        members = [context.artifacts[i] for i in topic.artifact_ids if i in context.artifacts]
        exclusivity, why_exclusive = _exclusivity(topic, members, context)
        doc_gap, status, pages, why_doc = _doc_gap(topic, context)
        recency, why_recent = _recency(members, as_of)
        criticality, why_critical = _criticality(members)
        score = 100 * exclusivity * recency * (0.5 * doc_gap + 0.5 * criticality)
        scored.append(
            GapScore(
                topic_id=topic.id,
                exclusivity=round(exclusivity, 4),
                doc_gap=doc_gap,
                recency=round(recency, 4),
                criticality=criticality,
                score=round(score, 2),
                doc_status=status,
                linked_documents=tuple(page.id for page in pages),
                explanation={
                    "exclusivity": why_exclusive,
                    "doc_gap": why_doc,
                    "recency": why_recent,
                    "criticality": why_critical,
                    "score": (
                        f"100 x {exclusivity:.2f} x {recency:.2f} x "
                        f"(0.5 x {doc_gap:.2f} + 0.5 x {criticality:.2f}) = {score:.1f}"
                    ),
                },
            )
        )
    return tuple(sorted(scored, key=lambda s: (-s.score, s.topic_id)))
