"""Deliverable h (F4): artifacts are grouped into systems, incidents, procedures and decisions."""

import random
from typing import Any

from eki.ingest.embedding import HashingEmbedder
from eki.mapping.models import Topic, TopicKind
from eki.mapping.topics import build_topics
from tests.builders import commit, document, pull_request, snapshot, ticket

EMBEDDER = HashingEmbedder()


def topics_of(kind: TopicKind, topics: tuple[Topic, ...]) -> list[Topic]:
    return [t for t in topics if t.kind is kind]


def build(artifacts: list[Any]) -> tuple[Topic, ...]:
    return build_topics(snapshot(artifacts), EMBEDDER)


def test_empty_snapshot_gives_no_topics() -> None:
    assert build([]) == ()


def test_one_system_topic_per_system() -> None:
    topics = build(
        [
            commit("c1", "billing-batch", "fix: a"),
            commit("c2", "billing-batch", "feat: b"),
            ticket("T1", "ledger-db", "Replica lag"),
            commit("c3", "gateway", "fix: c"),
        ]
    )
    systems = topics_of(TopicKind.SYSTEM, topics)
    assert sorted(t.system or "" for t in systems) == ["billing-batch", "gateway", "ledger-db"]
    billing = next(t for t in systems if t.system == "billing-batch")
    assert billing.artifact_ids == ("c1", "c2")
    assert billing.label == "billing-batch"


def test_documents_do_not_create_or_join_topics() -> None:
    topics = build([document("x", "billing-batch deploy", "body"), commit("c1", "gateway", "m")])
    assert [t.system for t in topics_of(TopicKind.SYSTEM, topics)] == ["gateway"]
    assert all("WIKI-x" not in t.artifact_ids for t in topics)


def test_recurring_incident_titles_form_one_topic() -> None:
    title = "billing-batch run failed during the posting phase"
    topics = build([ticket(f"T{n}", "billing-batch", title) for n in range(1, 4)])
    (incident,) = topics_of(TopicKind.INCIDENT, topics)
    assert incident.artifact_ids == ("T1", "T2", "T3")
    assert incident.label == title
    assert incident.system == "billing-batch"


def test_near_identical_titles_merge() -> None:
    topics = build(
        [
            ticket("T1", "gateway", "Gateway rate-limit rollout for the starter tier", kind="task"),
            ticket("T2", "gateway", "Gateway rate-limit rollout for the partner tier", kind="task"),
            ticket(
                "T3", "gateway", "Gateway rate-limit rollout for the internal tier", kind="task"
            ),
        ]
    )
    (procedure,) = topics_of(TopicKind.PROCEDURE, topics)
    assert procedure.artifact_ids == ("T1", "T2", "T3")


def test_unrelated_titles_do_not_merge() -> None:
    topics = build(
        [
            ticket("T1", "gateway", "Intermittent 502 from the canary pool"),
            ticket("T2", "gateway", "Intermittent 502 from the canary pool"),
            ticket("T3", "gateway", "Add a regional breakdown to the tax export"),
            ticket("T4", "gateway", "Add a regional breakdown to the tax export"),
        ]
    )
    incidents = topics_of(TopicKind.INCIDENT, topics)
    assert sorted(t.artifact_ids for t in incidents) == [("T1", "T2"), ("T3", "T4")]


def test_single_ticket_makes_no_incident_topic() -> None:
    topics = build([ticket("T1", "gateway", "Something happened once")])
    assert topics_of(TopicKind.INCIDENT, topics) == []
    assert len(topics_of(TopicKind.SYSTEM, topics)) == 1


def test_same_title_in_different_systems_does_not_merge() -> None:
    topics = build(
        [
            ticket("T1", "gateway", "Disk almost full"),
            ticket("T2", "ledger-db", "Disk almost full"),
        ]
    )
    assert topics_of(TopicKind.INCIDENT, topics) == []


def test_incidents_and_tasks_with_the_same_title_are_one_incident_topic() -> None:
    """The same work filed as incidents and as tasks is asked about once, not twice."""
    topics = build(
        [
            ticket("T1", "gateway", "Rotate the certificate", kind="incident"),
            ticket("T2", "gateway", "Rotate the certificate", kind="incident"),
            ticket("T3", "gateway", "Rotate the certificate", kind="task"),
            ticket("T4", "gateway", "Rotate the certificate", kind="request"),
        ]
    )
    (incident,) = topics_of(TopicKind.INCIDENT, topics)
    assert incident.artifact_ids == ("T1", "T2", "T3", "T4")
    assert incident.label == "Rotate the certificate"
    assert topics_of(TopicKind.PROCEDURE, topics) == []


def test_incidents_and_tasks_with_different_titles_stay_separate() -> None:
    topics = build(
        [
            ticket("T1", "gateway", "Rotate the certificate", kind="incident"),
            ticket("T2", "gateway", "Rotate the certificate", kind="incident"),
            ticket("T3", "gateway", "Add a request-id header to upstream calls", kind="task"),
            ticket("T4", "gateway", "Add a request-id header to upstream calls", kind="task"),
        ]
    )
    assert [t.artifact_ids for t in topics_of(TopicKind.INCIDENT, topics)] == [("T1", "T2")]
    assert [t.artifact_ids for t in topics_of(TopicKind.PROCEDURE, topics)] == [("T3", "T4")]


def test_recurring_single_path_commits_form_a_procedure() -> None:
    commits = [
        commit(
            f"c{n}",
            "billing-batch",
            f"chore(flags): {verb} settlement_hold",
            ("config/flags.yaml",),
        )
        for n, verb in enumerate(["enable", "disable", "enable", "disable"])
    ]
    (procedure,) = topics_of(TopicKind.PROCEDURE, build(commits))
    assert procedure.artifact_ids == ("c0", "c1", "c2", "c3")
    assert procedure.label.startswith("config/flags.yaml: ")
    assert procedure.system == "billing-batch"


def test_digits_in_messages_are_ignored_when_comparing() -> None:
    commits = [
        commit(
            f"c{n}", "ledger-db", f"chore(deps): bump orjson from 3.{n}.0 to 3.{n}.1", ("lock.txt",)
        )
        for n in range(1, 4)
    ]
    assert len(topics_of(TopicKind.PROCEDURE, build(commits))) == 1


def test_varied_messages_on_one_path_do_not() -> None:
    commits = [
        commit("c1", "gateway", "fix: handle empty ledger pages", ("export/writer.py",)),
        commit("c2", "gateway", "feat: regional tax columns", ("export/writer.py",)),
        commit("c3", "gateway", "perf: stream rows instead of buffering", ("export/writer.py",)),
    ]
    assert topics_of(TopicKind.PROCEDURE, build(commits)) == []


def test_two_commits_are_not_enough_for_a_procedure() -> None:
    commits = [commit(f"c{n}", "gateway", "same message", ("a.py",)) for n in range(2)]
    assert topics_of(TopicKind.PROCEDURE, build(commits)) == []


def test_commits_touching_several_paths_are_not_procedure_candidates() -> None:
    commits = [commit(f"c{n}", "gateway", "same message", ("a.py", "b.py")) for n in range(4)]
    assert topics_of(TopicKind.PROCEDURE, build(commits)) == []


def test_rejected_review_makes_a_decision_topic() -> None:
    topics = build(
        [
            pull_request("PR-1", "billing-batch", "Simplify period rollover", "rejected"),
            pull_request("PR-2", "billing-batch", "Ask for edits", "changes_requested"),
            pull_request("PR-3", "billing-batch", "Fine change", "approved"),
        ]
    )
    decisions = topics_of(TopicKind.DECISION, topics)
    assert sorted(t.artifact_ids for t in decisions) == [("PR-1",), ("PR-2",)]
    assert (
        next(t for t in decisions if t.artifact_ids == ("PR-1",)).label
        == "Simplify period rollover"
    )


def test_approved_review_does_not() -> None:
    topics = build([pull_request("PR-3", "billing-batch", "Fine change", "approved")])
    assert topics_of(TopicKind.DECISION, topics) == []


def test_output_is_deterministic_and_sorted() -> None:
    artifacts = [
        *(ticket(f"T{n}", "billing-batch", "run failed in posting") for n in range(4)),
        *(commit(f"c{n}", "gateway", "chore: bump x", ("l.txt",)) for n in range(4)),
        pull_request("PR-1", "gateway", "Rejected one", "rejected"),
        ticket("T9", "ledger-db", "Replica lag"),
    ]
    baseline = build(artifacts)
    for seed in range(5):
        shuffled = artifacts[:]
        random.Random(seed).shuffle(shuffled)
        assert build(shuffled) == baseline
    assert [t.id for t in baseline] == sorted(t.id for t in baseline)
    assert len({t.id for t in baseline}) == len(baseline)


def test_topic_ids_are_stable_and_readable() -> None:
    topics = build([ticket(f"T{n}", "billing-batch", "Run failed in posting") for n in range(2)])
    ids = {t.id for t in topics}
    assert "system:billing-batch:billing-batch" in ids
    assert "incident:billing-batch:run-failed-in-posting" in ids
