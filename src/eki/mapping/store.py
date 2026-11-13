"""Save and load topics and gap scores for one ingest run."""

from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from eki.mapping.models import DocStatus, GapScore, Topic, TopicKind


def save_mapping(
    conn: psycopg.Connection, run_id: UUID, topics: tuple[Topic, ...], scores: tuple[GapScore, ...]
) -> None:
    """Replace the run's topics and scores, atomically. Idempotent."""
    with conn.transaction():
        conn.execute("DELETE FROM topics WHERE run_id = %s", (run_id,))
        with conn.cursor() as cursor:
            cursor.executemany(
                "INSERT INTO topics (run_id, id, kind, system, label) VALUES (%s, %s, %s, %s, %s)",
                [(run_id, t.id, t.kind.value, t.system, t.label) for t in topics],
            )
            cursor.executemany(
                "INSERT INTO topic_artifacts (run_id, topic_id, artifact_id) VALUES (%s, %s, %s)",
                [(run_id, t.id, artifact_id) for t in topics for artifact_id in t.artifact_ids],
            )
            cursor.executemany(
                "INSERT INTO gap_scores (run_id, topic_id, exclusivity, doc_gap, recency, "
                "criticality, score, doc_status, linked_documents, explanation) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                [
                    (
                        run_id,
                        s.topic_id,
                        s.exclusivity,
                        s.doc_gap,
                        s.recency,
                        s.criticality,
                        s.score,
                        s.doc_status.value,
                        Jsonb(list(s.linked_documents)),
                        Jsonb(s.explanation),
                    )
                    for s in scores
                ],
            )


def load_mapping(
    conn: psycopg.Connection, run_id: UUID
) -> tuple[tuple[Topic, ...], tuple[GapScore, ...]]:
    """Topics sorted by ID, scores sorted by score (highest first) then topic ID."""
    members: dict[str, list[str]] = {}
    for topic_id, artifact_id in conn.execute(
        "SELECT topic_id, artifact_id FROM topic_artifacts WHERE run_id = %s "
        'ORDER BY topic_id COLLATE "C", artifact_id COLLATE "C"',
        (run_id,),
    ).fetchall():
        members.setdefault(topic_id, []).append(artifact_id)
    topic_rows = conn.execute(
        'SELECT id, kind, system, label FROM topics WHERE run_id = %s ORDER BY id COLLATE "C"',
        (run_id,),
    ).fetchall()
    topics = tuple(
        Topic(
            id=r[0],
            kind=TopicKind(r[1]),
            system=r[2],
            label=r[3],
            artifact_ids=tuple(members.get(r[0], [])),
        )
        for r in topic_rows
    )
    score_rows = conn.execute(
        "SELECT topic_id, exclusivity, doc_gap, recency, criticality, score, doc_status, "
        "linked_documents, explanation FROM gap_scores WHERE run_id = %s "
        'ORDER BY score DESC, topic_id COLLATE "C"',
        (run_id,),
    ).fetchall()
    scores = tuple(
        GapScore(
            topic_id=r[0],
            exclusivity=r[1],
            doc_gap=r[2],
            recency=r[3],
            criticality=r[4],
            score=r[5],
            doc_status=DocStatus(r[6]),
            linked_documents=tuple(r[7]),
            explanation=r[8],
        )
        for r in score_rows
    )
    return topics, scores
