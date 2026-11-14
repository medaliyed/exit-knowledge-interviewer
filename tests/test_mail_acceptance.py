"""F3: mail-only knowledge becomes reachable without ingesting private data."""

import json
from dataclasses import replace
from uuid import UUID

import psycopg
import pytest

from eki.ingest.embedding import HashingEmbedder
from eki.ingest.index import index_run
from eki.ingest.pipeline import ingest_from_source, load, store
from eki.mapping.models import TopicKind
from eki.mapping.scoring import score_topics
from eki.mapping.topics import build_topics
from tests.fixtures.generator.world import HOME_ADDRESS, PHONE_NUMBER
from tests.support import CORPUS, FAKE_CREDENTIALS, FIXTURES, FULL_SPAN

pytestmark = [pytest.mark.anyio, pytest.mark.eval]


async def test_mail_reveals_backup_decision_and_recurring_vendor_contact() -> None:
    scope = replace(FULL_SPAN, mail_folders=("Inbox", "Sent", "Vendors"))
    snapshot = await ingest_from_source(scope, CORPUS)
    key = json.loads((FIXTURES / "answer_key.json").read_text())
    present = {a.id for a in snapshot.artifacts}
    assert not set(key["excluded_mail_ids"]) & present
    assert all(
        secret not in snapshot.model_dump_json()
        for secret in (*FAKE_CREDENTIALS, HOME_ADDRESS, PHONE_NUMBER)
    )
    topics = build_topics(snapshot, HashingEmbedder())
    scores = score_topics(topics, snapshot, as_of=snapshot.until)
    assert {s.topic_id for s in scores} == {t.id for t in topics}
    for gap, kind in (("G8", TopicKind.DECISION), ("G4", TopicKind.CONTACT)):
        wanted = set(key["gaps"][gap]["artifact_ids"])
        candidates = [t for t in topics if t.kind == kind and wanted <= set(t.artifact_ids)]
        assert candidates, (gap, wanted)
    assert any(
        "overlaps the settlement window" in a.body for a in snapshot.artifacts if a.kind == "email"
    )


@pytest.mark.db
async def test_mail_snapshot_and_consent_round_trip(conn: psycopg.Connection) -> None:
    snapshot = await ingest_from_source(replace(FULL_SPAN, mail_folders=("Inbox", "Sent")), CORPUS)
    run_id: UUID = store(conn, snapshot)
    assert load(conn, run_id) == snapshot
    assert index_run(conn, run_id, HashingEmbedder()) > 0
    bodies = conn.execute("SELECT text FROM chunks WHERE run_id = %s", (run_id,)).fetchall()
    assert all(secret not in str(bodies) for secret in FAKE_CREDENTIALS)
