"""F22 9c: entry proposals, source lineage, correction propagation and G7 gap resolution."""

from typing import Any
from uuid import UUID

import pytest

from eki.followup.gaps import GapService, open_section_gap_id
from eki.runbook.review import SectionEdit
from eki.runbook.service import RunbookService
from tests.conftest import Scratch
from tests.followup_support import OPEN_REASON, answered_follow_up, gap_world
from tests.search_support import OWNER

pytestmark = [pytest.mark.db, pytest.mark.anyio]


def queue(scratch: Scratch, run_id: UUID, filter: str = "open") -> dict[str, dict[str, Any]]:
    listed = GapService(scratch.dsn).queue(run_id, OWNER, filter=filter)["gaps"]
    return {g["id"]: g for g in listed}


# --- G7 / A1 / A10: the queue keeps answered work visible ---


async def test_queue_keeps_answered_open_section_after_section_is_filled(
    scratch: Scratch,
) -> None:
    done = await answered_follow_up(scratch)
    world = done.world
    before = queue(scratch, world.run_id)[done.rollback_gap]
    assert before["status"] == "awaiting_review" and before["live"] is True
    books = RunbookService(scratch.dsn)
    books.edit(world.billing, OWNER.name, {"rollback": SectionEdit(claims=("Restore export.",))})
    after = queue(scratch, world.run_id)
    assert done.rollback_gap in after, "answered work must not vanish when its section fills"
    gap = after[done.rollback_gap]
    assert gap["status"] == "awaiting_review" and gap["live"] is False
    assert gap["open_question"] is None and gap["entry_title"]
    assert gap["entry_status"] == "draft" and gap["section"] == "rollback"
    assert [a["outcome"] for a in gap["attempts"]] == ["answered"]


async def test_non_live_pending_open_section_stays_hidden(scratch: Scratch) -> None:
    world = await gap_world(scratch)
    service = GapService(scratch.dsn)
    gap_id = str(open_section_gap_id(world.billing, "rollback"))
    deferred = service.triage(
        world.run_id, gap_id, OWNER, action="defer", expected_revision=0, reason="Later"
    )
    service.triage(
        world.run_id, gap_id, OWNER, action="reopen", expected_revision=deferred["revision"]
    )
    RunbookService(scratch.dsn).edit(
        world.billing, OWNER.name, {"rollback": SectionEdit(claims=("Restore export.",))}
    )
    assert queue(scratch, world.run_id, "all") == {}


async def test_reopened_section_after_resolution_is_triageable_again(scratch: Scratch) -> None:
    world = await gap_world(scratch)
    service = GapService(scratch.dsn)
    gap_id = str(open_section_gap_id(world.billing, "rollback"))
    books = RunbookService(scratch.dsn)
    books.edit(world.billing, OWNER.name, {"rollback": SectionEdit(claims=("Restore export.",))})
    stored = books.approve(world.billing, OWNER.name)
    with scratch.connect() as conn:
        conn.execute(
            "INSERT INTO follow_up_gaps(id,run_id,kind,entry_id,section,created_at,status,"
            "revision,resolved_entry_id,resolved_version,resolved_at,resolved_by_subject) "
            "VALUES (%s,%s,'open_section',%s,'rollback',now(),'resolved',3,%s,%s,now(),%s)",
            (UUID(gap_id), world.run_id, world.billing, world.billing, stored.version, "nadia"),
        )
    resolved = queue(scratch, world.run_id, "all")[gap_id]
    assert resolved["status"] == "resolved" and resolved["live"] is False
    assert queue(scratch, world.run_id).get(gap_id) is None, "resolved is not open"
    books.edit(world.billing, OWNER.name, {"rollback": SectionEdit(open_question=OPEN_REASON)})
    reopened = queue(scratch, world.run_id)[gap_id]
    assert reopened["status"] == "pending" and reopened["reopened"] is True
    assert reopened["live"] is True and reopened["resolved_version"] == stored.version
    assert reopened["revision"] == 3
    triaged = service.triage(
        world.run_id, gap_id, OWNER, action="defer", expected_revision=3, reason="Not now"
    )
    assert triaged["status"] == "deferred" and triaged["revision"] == 4
    assert triaged["resolved_version"] is None and triaged["resolved_at"] is None
