"""Deliverable i (F5): a gap score from exclusivity, documentation, recency and criticality."""

from datetime import timedelta
from typing import Any

import pytest

from eki.ingest.embedding import HashingEmbedder
from eki.mapping.models import DocStatus, GapScore, Topic, TopicKind
from eki.mapping.scoring import score_topics
from eki.mapping.topics import build_topics
from tests.builders import (
    AS_OF,
    activity,
    commit,
    document,
    pull_request,
    snapshot,
    ticket,
    when,
)

EMBEDDER = HashingEmbedder()
FAILED = "billing-batch run failed during the posting phase"


def scores_for(artifacts: list[Any], activities: list[Any] | None = None) -> dict[str, GapScore]:
    snap = snapshot(artifacts, activities)
    topics = build_topics(snap, EMBEDDER)
    return {s.topic_id: s for s in score_topics(topics, snap, as_of=AS_OF)}


def incident_tickets(system: str, title: str, count: int, **kwargs: Any) -> list[Any]:
    return [ticket(f"{system}-{title[:3]}-{n}", system, title, **kwargs) for n in range(count)]


def group(
    system: str, title: str, total: int, by_expert: int, others: int = 0, kind: str = "incident"
) -> Any:
    return activity("ticket_group", system, f"{kind}|{title}", total, by_expert, others)


def test_sole_handler_scores_above_shared_work() -> None:
    tickets = [
        *incident_tickets("billing-batch", FAILED, 3),
        *incident_tickets("gateway", "Intermittent 502 from the canary pool", 3),
    ]
    scores = scores_for(
        tickets,
        [
            group("billing-batch", FAILED, 3, 3),
            group("gateway", "Intermittent 502 from the canary pool", 9, 3, 2),
        ],
    )
    sole = scores[f"incident:billing-batch:{'billing-batch-run-failed-during-the-posting-phase'}"]
    shared = scores["incident:gateway:intermittent-502-from-the-canary-pool"]
    assert sole.exclusivity == 1.0
    assert shared.exclusivity == pytest.approx(1 / 3, abs=1e-3)
    assert sole.score > shared.score


def test_zero_exclusivity_scores_zero() -> None:
    scores = scores_for(
        incident_tickets("gateway", "Disk almost full", 2),
        [group("gateway", "Disk almost full", 4, 0, 3)],
    )
    assert scores["incident:gateway:disk-almost-full"].score == 0.0


def test_documented_topic_scores_below_undocumented() -> None:
    page = document("failover", "ledger-db failover", "Steps to fail over.")
    tickets = incident_tickets("ledger-db", "ledger-db primary failover after storage alarm", 3)
    with_page = scores_for([*tickets, page], [group("ledger-db", tickets[0].title, 3, 3)])
    without_page = scores_for(tickets, [group("ledger-db", tickets[0].title, 3, 3)])
    topic_id = "incident:ledger-db:ledger-db-primary-failover-after-storage-alarm"
    assert with_page[topic_id].doc_status is DocStatus.DOCUMENTED
    assert with_page[topic_id].doc_gap == 0.0
    assert without_page[topic_id].doc_status is DocStatus.NONE
    assert without_page[topic_id].doc_gap == 1.0
    assert without_page[topic_id].score > with_page[topic_id].score
    assert with_page[topic_id].linked_documents == ("WIKI-failover",)


def test_stale_page_gives_partial() -> None:
    page = document(
        "deploy",
        "billing-batch deploy and schedule",
        "The batch starts from `deploy/cron.yaml`.",
        last_updated="2025-04-15",
    )
    changed = activity("path", "billing-batch", "deploy/cron.yaml", 2, 0, 2, when(2025, 12, 9))
    scores = scores_for(
        [page, commit("c1", "billing-batch", "fix: x")],
        [changed, activity("repo", "billing-batch", "billing-batch", 5, 1, 2)],
    )
    system_score = scores["system:billing-batch:billing-batch"]
    assert system_score.doc_status is DocStatus.PARTIAL
    assert system_score.doc_gap == 0.5
    assert "deploy/cron.yaml" in system_score.explanation["doc_gap"]


def test_page_with_unchanged_paths_is_documented() -> None:
    page = document(
        "deploy",
        "billing-batch deploy and schedule",
        "The batch starts from `deploy/cron.yaml`.",
        last_updated="2025-04-15",
    )
    unchanged = activity("path", "billing-batch", "deploy/cron.yaml", 1, 0, 1, when(2025, 1, 2))
    scores = scores_for(
        [page, commit("c1", "billing-batch", "fix: x")],
        [unchanged, activity("repo", "billing-batch", "billing-batch", 5, 1, 2)],
    )
    assert scores["system:billing-batch:billing-batch"].doc_status is DocStatus.DOCUMENTED


def test_page_links_only_within_its_system() -> None:
    page = document("cert", "Gateway certificate renewal", "Steps.")
    title = "Gateway TLS certificate renewal"
    scores = scores_for(
        [
            *incident_tickets("gateway", title, 2, kind="task"),
            *incident_tickets("ledger-db", title, 2, kind="task"),
            page,
        ],
        [group("gateway", title, 2, 2, kind="task"), group("ledger-db", title, 2, 2, kind="task")],
    )
    assert (
        scores["procedure:gateway:gateway-tls-certificate-renewal"].doc_status
        is DocStatus.DOCUMENTED
    )
    assert (
        scores["procedure:ledger-db:gateway-tls-certificate-renewal"].doc_status is DocStatus.NONE
    )


def test_page_needs_most_of_its_title_words_in_the_label() -> None:
    page = document("cert", "Gateway certificate renewal procedure checklist", "Steps.")
    title = "Gateway certificate expiry"
    scores = scores_for(
        incident_tickets("gateway", title, 2, kind="task"),
        [group("gateway", title, 2, 2, kind="task")],
    )
    assert (
        scores_for(
            [*incident_tickets("gateway", title, 2, kind="task"), page],
            [group("gateway", title, 2, 2, kind="task")],
        )["procedure:gateway:gateway-certificate-expiry"].doc_status
        is DocStatus.NONE
    )
    assert scores["procedure:gateway:gateway-certificate-expiry"].linked_documents == ()


def test_system_topic_links_every_page_of_its_system() -> None:
    pages = [
        document("a", "gateway certificate renewal", "x"),
        document("b", "gateway rate-limit rollout", "x"),
        document("c", "ledger-db failover", "x"),
    ]
    scores = scores_for([commit("c1", "gateway", "fix: x"), *pages])
    assert scores["system:gateway:gateway"].linked_documents == ("WIKI-a", "WIKI-b")


@pytest.mark.parametrize(("months", "expected"), [(0, 1.0), (12, 0.5), (24, 0.25), (36, 0.125)])
def test_old_activity_decays(months: int, expected: float) -> None:
    last = AS_OF - timedelta(days=months * 30.4375)
    scores = scores_for([commit("c1", "gateway", "fix: x", at=last)])
    assert scores["system:gateway:gateway"].recency == pytest.approx(expected, abs=0.005)


def test_recency_uses_the_newest_artifact_in_the_topic() -> None:
    old, new = AS_OF - timedelta(days=900), AS_OF - timedelta(days=30)
    scores = scores_for(
        [commit("c1", "gateway", "a", at=old), commit("c2", "gateway", "b", at=new)]
    )
    assert scores["system:gateway:gateway"].recency > 0.9


@pytest.mark.parametrize(
    ("priority", "expected"), [("P1", 1.0), ("P2", 0.75), ("P3", 0.5), ("P4", 0.25)]
)
def test_priority_sets_criticality(priority: str, expected: float) -> None:
    scores = scores_for(incident_tickets("gateway", "Disk full", 2, priority=priority))
    assert scores["incident:gateway:disk-full"].criticality == expected


def test_highest_priority_in_a_topic_wins() -> None:
    tickets = [
        ticket("T1", "gateway", "Disk full", priority="P3"),
        ticket("T2", "gateway", "Disk full", priority="P1"),
    ]
    assert scores_for(tickets)["incident:gateway:disk-full"].criticality == 1.0


def test_topics_without_tickets_have_middling_criticality() -> None:
    scores = scores_for([commit("c1", "gateway", "fix: x")])
    assert scores["system:gateway:gateway"].criticality == 0.5


def test_decision_topics_are_fully_exclusive() -> None:
    scores = scores_for([pull_request("PR-1", "billing-batch", "Simplify rollover", "rejected")])
    assert scores["decision:billing-batch:simplify-rollover"].exclusivity == 1.0


def test_system_exclusivity_comes_from_repo_activity() -> None:
    scores = scores_for(
        [commit("c1", "gateway", "fix: x")],
        [activity("repo", "gateway", "gateway", 40, 10, 3)],
    )
    assert scores["system:gateway:gateway"].exclusivity == pytest.approx(0.25)


def test_commit_procedure_exclusivity_comes_from_path_activity() -> None:
    commits = [
        commit(f"c{n}", "billing-batch", "chore(flags): enable flag", ("config/flags.yaml",))
        for n in range(3)
    ]
    scores = scores_for(
        commits,
        [
            activity("path", "billing-batch", "config/flags.yaml", 4, 3, 1),
            activity("repo", "billing-batch", "billing-batch", 10, 3, 2),
        ],
    )
    procedure = next(s for s in scores.values() if s.topic_id.startswith("procedure:"))
    assert procedure.exclusivity == pytest.approx(0.75)


def test_missing_activity_is_called_out_in_the_explanation() -> None:
    scores = scores_for(incident_tickets("gateway", "Disk full", 2))
    assert (
        "no activity data"
        in scores["incident:gateway:disk-full"].explanation["exclusivity"].lower()
    )


def test_explanation_states_the_numbers() -> None:
    tickets = incident_tickets("billing-batch", FAILED, 5, priority="P1")
    scores = scores_for(tickets, [group("billing-batch", FAILED, 5, 5)])
    explanation = next(s for s in scores.values() if s.topic_id.startswith("incident:")).explanation
    assert set(explanation) == {"exclusivity", "doc_gap", "recency", "criticality", "score"}
    assert "5 of 5" in explanation["exclusivity"]
    assert "P1" in explanation["criticality"]
    assert "No wiki page" in explanation["doc_gap"]
    assert "months" in explanation["recency"]


def test_score_formula() -> None:
    tickets = incident_tickets("gateway", "Disk full", 2, priority="P2")
    scores = scores_for(tickets, [group("gateway", "Disk full", 4, 2, 1)])
    s = scores["incident:gateway:disk-full"]
    expected = 100 * s.exclusivity * s.recency * (0.5 * s.doc_gap + 0.5 * s.criticality)
    assert s.score == pytest.approx(expected, abs=0.01)
    assert 0 <= s.score <= 100
    assert all(0 <= v <= 1 for v in (s.exclusivity, s.doc_gap, s.recency, s.criticality))


def test_scores_are_sorted_and_stable() -> None:
    artifacts = [
        *incident_tickets("billing-batch", FAILED, 3, priority="P1"),
        *incident_tickets("gateway", "Disk almost full", 2, priority="P4"),
        commit("c1", "ledger-db", "fix: x"),
    ]
    activities = [
        group("billing-batch", FAILED, 3, 3),
        group("gateway", "Disk almost full", 6, 2, 3),
    ]
    snap = snapshot(artifacts, activities)
    topics = build_topics(snap, EMBEDDER)
    first = score_topics(topics, snap, as_of=AS_OF)
    assert first == score_topics(tuple(reversed(topics)), snap, as_of=AS_OF)
    ordering = [(-s.score, s.topic_id) for s in first]
    assert ordering == sorted(ordering)


def test_unknown_artifact_ids_are_ignored_gracefully() -> None:
    snap = snapshot([commit("c1", "gateway", "fix: x")])
    ghost = Topic(
        id="incident:gateway:ghost",
        kind=TopicKind.INCIDENT,
        system="gateway",
        label="ghost",
        artifact_ids=("nope",),
    )
    result = score_topics((ghost,), snap, as_of=AS_OF)
    assert result[0].score == 0.0
