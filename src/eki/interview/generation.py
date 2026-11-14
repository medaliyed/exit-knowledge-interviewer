"""Persist immutable per-topic drafting results independently of graph checkpoints."""

import hashlib
import json
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from eki.audit import AuditDetails, append_event
from eki.errors import ConflictError, NotFoundError
from eki.interview import store
from eki.interview.models import InterviewPlan, PlannedTopic

Status = Literal["running", "needs_retry", "completed"]


def fingerprint(value: dict[str, Any]) -> str:
    # Capture prompt/validation/selection code as well as configured model and inputs.
    source = b"".join(
        (Path(__file__).parent / name).read_bytes()
        for name in ("drafting.py", "conflicts.py", "plan.py")
    )
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(source + canonical.encode()).hexdigest()


def owner_run(conn: psycopg.Connection, gid: UUID) -> UUID | None:
    row = conn.execute("SELECT run_id FROM plan_generations WHERE id=%s", (gid,)).fetchone()
    return None if row is None else UUID(str(row[0]))


def set_status(conn: psycopg.Connection, gid: UUID, status: Status) -> None:
    with conn.transaction():
        row = conn.execute(
            "UPDATE plan_generations SET status=%s,updated_at=now() WHERE id=%s RETURNING run_id",
            (status, gid),
        ).fetchone()
        if row is not None:
            count = conn.execute(
                "SELECT count(*) FROM generation_topics WHERE generation_id=%s", (gid,)
            ).fetchone()
            append_event(
                conn,
                "generation",
                "completed"
                if status == "completed"
                else ("failed" if status == "needs_retry" else "started"),
                AuditDetails(
                    operation="generate_plan",
                    target_id=gid,
                    generation_status=status,
                    count=0 if count is None else count[0],
                ),
                run_id=row[0],
            )


def begin(conn: psycopg.Connection, plan: InterviewPlan, digest: str) -> None:
    with conn.transaction():
        row = conn.execute(
            "SELECT fingerprint,run_id FROM plan_generations WHERE id=%s", (plan.id,)
        ).fetchone()
        if row is not None and (row[0] != digest or row[1] != plan.run_id):
            raise ConflictError(
                "generation inputs or configuration changed; start a new generation"
            )
        if row is None:
            conn.execute(
                "INSERT INTO plan_generations (id,run_id,fingerprint,selected_plan,status) "
                "VALUES (%s,%s,%s,%s,'running')",
                (plan.id, plan.run_id, digest, Jsonb(plan.model_dump(mode="json"))),
            )
        set_status(conn, plan.id, "running")


def completed_topics(conn: psycopg.Connection, gid: UUID) -> dict[str, PlannedTopic]:
    return {
        row[0]: PlannedTopic.model_validate(row[1])
        for row in conn.execute(
            "SELECT topic_id,result FROM generation_topics WHERE generation_id=%s", (gid,)
        ).fetchall()
    }


def save_topic(conn: psycopg.Connection, gid: UUID, topic: PlannedTopic) -> None:
    with conn.transaction():
        conn.execute(
            "INSERT INTO generation_topics (generation_id,topic_id,result) "
            "VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
            (gid, topic.topic_id, Jsonb(topic.model_dump(mode="json"))),
        )


def read(
    conn: psycopg.Connection, run_id: UUID, gid: UUID, *, detect_abandoned: bool = True
) -> dict[str, Any]:
    row = conn.execute(
        "SELECT status,selected_plan FROM plan_generations WHERE id=%s AND run_id=%s", (gid, run_id)
    ).fetchone()
    if row is None:
        raise NotFoundError("plan generation not found")
    if detect_abandoned and row[0] == "running" and store.try_lock(conn, gid):
        # Reload after acquiring the lock: the previous worker may just have completed.
        row = conn.execute(
            "SELECT status,selected_plan FROM plan_generations WHERE id=%s", (gid,)
        ).fetchone()
        assert row is not None  # noqa: S101
        if row[0] == "running":
            set_status(conn, gid, "needs_retry")
            row = ("needs_retry", row[1])
    plan = InterviewPlan.model_validate(row[1])
    results = completed_topics(conn, gid)
    return {
        "id": str(gid),
        "run_id": str(run_id),
        "status": row[0],
        "completed": len(results),
        "total": len(plan.topics),
        "plan_id": str(gid) if row[0] == "completed" else None,
        "topics": [
            {
                **results.get(t.topic_id, t).model_dump(mode="json"),
                "generation_state": "completed" if t.topic_id in results else "pending",
            }
            for t in plan.topics
        ],
    }


def listing(conn: psycopg.Connection, run_id: UUID) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT id FROM plan_generations WHERE run_id=%s ORDER BY created_at DESC,id LIMIT 200",
        (run_id,),
    ).fetchall()
    # A fresh lock owner is required for each abandonment check; list views are read-only.
    return [read(conn, run_id, row[0], detect_abandoned=False) for row in rows]


def mark_retry(conn: psycopg.Connection, run_id: UUID, gid: UUID) -> None:
    """Do not demote published results or mutate another run on an error path."""
    with conn.transaction():
        row = conn.execute(
            "SELECT status FROM plan_generations WHERE id=%s AND run_id=%s FOR UPDATE",
            (gid, run_id),
        ).fetchone()
        if row is not None and row[0] != "completed":
            set_status(conn, gid, "needs_retry")
