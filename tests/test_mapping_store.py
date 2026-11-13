"""Deliverable i: topics and scores survive a round trip through Postgres."""

import psycopg
import pytest

from eki.ingest.embedding import HashingEmbedder
from eki.ingest.models import Snapshot
from eki.ingest.pipeline import store
from eki.mapping.scoring import score_topics
from eki.mapping.store import load_mapping, save_mapping
from eki.mapping.topics import build_topics
from tests.builders import AS_OF

pytestmark = [pytest.mark.db, pytest.mark.anyio]


async def test_topics_and_scores_round_trip(conn: psycopg.Connection, snapshot: Snapshot) -> None:
    run_id = store(conn, snapshot)
    topics = build_topics(snapshot, HashingEmbedder())
    scores = score_topics(topics, snapshot, as_of=AS_OF)
    save_mapping(conn, run_id, topics, scores)
    loaded_topics, loaded_scores = load_mapping(conn, run_id)
    assert loaded_topics == topics
    assert loaded_scores == scores
    assert len(topics) > 20


async def test_saving_twice_replaces_rather_than_duplicates(
    conn: psycopg.Connection, snapshot: Snapshot
) -> None:
    run_id = store(conn, snapshot)
    topics = build_topics(snapshot, HashingEmbedder())
    scores = score_topics(topics, snapshot, as_of=AS_OF)
    save_mapping(conn, run_id, topics, scores)
    save_mapping(conn, run_id, topics, scores)
    assert conn.execute("SELECT count(*) FROM topics").fetchone() == (len(topics),)
    assert conn.execute("SELECT count(*) FROM gap_scores").fetchone() == (len(scores),)


async def test_mapping_for_an_unmapped_run_is_empty(
    conn: psycopg.Connection, snapshot: Snapshot
) -> None:
    run_id = store(conn, snapshot)
    assert load_mapping(conn, run_id) == ((), ())
