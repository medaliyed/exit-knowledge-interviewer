"""F22 G7/A10: only reviewed public knowledge resolves a follow-up gap."""

import json
from threading import Barrier, Thread
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from eki import access, maintenance
from eki.api.app import create_app
from eki.audit import AuditError, append_event
from eki.auth import Principal, acting_as
from eki.errors import ConflictError, NotFoundError
from eki.followup import gaps as gap_module
from eki.followup.gaps import GapService
from eki.runbook.review import SectionEdit
from eki.runbook.service import RunbookService
from tests.conftest import Scratch
from tests.followup_support import OPEN_REASON, ROLLBACK_ANSWER, answered_follow_up, gap_world
from tests.interview_support import make_service
from tests.search_support import ADMIN, OWNER, READER, STRANGER
from tests.test_api_access import authenticator, headers
from tests.test_follow_up_proposals import apply_all, audit_details, queue

pytestmark = [pytest.mark.db, pytest.mark.anyio]


async def test_approval_resolves_public_section_but_request_needs_confirmation(
    scratch: Scratch,
) -> None:
    done = await answered_follow_up(scratch)
    world = done.world
    books, gaps = RunbookService(scratch.dsn), GapService(scratch.dsn)
    applied = apply_all(books, done)
    assert queue(scratch, world.run_id)[done.rollback_gap]["status"] == "awaiting_review"
    with acting_as(OWNER):
        approved = books.approve(world.billing, OWNER.name, expected_version=applied.version)
    states = queue(scratch, world.run_id, "all")
    resolved = states[done.rollback_gap]
    assert resolved["status"] == "resolved" and resolved["live"] is False
    assert resolved["resolved_entry_id"] == str(world.billing)
    assert resolved["resolved_version"] == approved.version
    assert resolved["resolved_by_subject"] == OWNER.subject
    assert resolved["resolved_at"]
    assert states[done.request_gap]["status"] == "awaiting_review"
    saved = gaps.resolve(
        world.run_id,
        done.request_gap,
        OWNER,
        expected_revision=states[done.request_gap]["revision"],
        entry_id=world.billing,
        expected_version=approved.version,
        confirms_answer=True,
    )
    assert saved["status"] == "resolved" and saved["resolved_version"] == approved.version
    assert gaps.receipt(done.request_gap, READER)["status"] == "resolved"
    assert "resolved_by_subject" not in gaps.receipt(done.request_gap, READER)
    # A delivery retry is idempotent, including the original expected revision.
    assert (
        gaps.resolve(
            world.run_id,
            done.request_gap,
            OWNER,
            expected_revision=states[done.request_gap]["revision"],
            entry_id=world.billing,
            expected_version=approved.version,
            confirms_answer=True,
        )
        == saved
    )
    events = audit_details(scratch, "gap_resolved")
    assert len(events) == 2 and all("question" not in json.dumps(e) for e in events)
    reopened = gaps.triage(
        world.run_id,
        done.request_gap,
        OWNER,
        action="reopen",
        expected_revision=saved["revision"],
    )
    assert reopened["status"] == "pending" and reopened["resolved_version"] is None
    with acting_as(OWNER):
        books.edit(world.billing, OWNER.name, {"rollback": SectionEdit(open_question=OPEN_REASON)})
    public_gap = queue(scratch, world.run_id)[done.rollback_gap]
    assert public_gap["status"] == "pending" and public_gap["reopened"] is True


async def test_unfilled_approved_section_never_resolves(scratch: Scratch) -> None:
    done = await answered_follow_up(scratch)
    books = RunbookService(scratch.dsn)
    # The proposal remains private; approving an unrelated hand edit closes no gap.
    with acting_as(OWNER):
        books.edit(done.world.billing, OWNER.name, {"contacts": SectionEdit(claims=("Finance.",))})
        books.approve(done.world.billing, OWNER.name)
    states = queue(scratch, done.world.run_id)
    assert states[done.rollback_gap]["status"] == "awaiting_review"
    assert states[done.request_gap]["status"] == "awaiting_review"


async def test_reopened_grouped_request_can_be_selected_independently(scratch: Scratch) -> None:
    world = await gap_world(scratch)
    gaps = GapService(scratch.dsn)
    first, _ = gaps.save_request(world.run_id, READER, request_id=uuid4(), question="Who signs?")
    second, _ = gaps.save_request(world.run_id, READER, request_id=uuid4(), question="Who checks?")
    grouped = gaps.triage(
        world.run_id,
        second["id"],
        OWNER,
        action="group",
        expected_revision=0,
        group_with=UUID(first["id"]),
    )
    approved = RunbookService(scratch.dsn).get(world.billing)
    resolved = gaps.resolve(
        world.run_id,
        second["id"],
        OWNER,
        expected_revision=grouped["revision"],
        entry_id=world.billing,
        expected_version=approved.version,
        confirms_answer=True,
    )
    reopened = gaps.triage(
        world.run_id,
        second["id"],
        OWNER,
        action="reopen",
        expected_revision=resolved["revision"],
    )
    assert reopened["status"] == "pending" and reopened["group_id"] is None


async def test_request_resolution_checks_authority_revision_and_public_target(
    scratch: Scratch,
) -> None:
    world = await gap_world(scratch)
    gaps, books = GapService(scratch.dsn), RunbookService(scratch.dsn)
    saved, _ = gaps.save_request(world.run_id, READER, request_id=uuid4(), question="Who signs?")
    base = books.get(world.billing)
    args: dict[str, Any] = {
        "expected_revision": saved["revision"],
        "entry_id": world.billing,
        "expected_version": base.version,
        "confirms_answer": True,
    }
    for who in (
        READER,
        STRANGER,
        Principal("impostor", OWNER.name),
        Principal("admin", OWNER.name, True),
    ):
        with pytest.raises(NotFoundError):
            gaps.resolve(world.run_id, saved["id"], who, **args)
    with pytest.raises(NotFoundError):
        gaps.resolve(world.run_id, saved["id"], ADMIN, **args)
    for override in ({"expected_revision": 9}, {"expected_version": base.version + 1}):
        with pytest.raises(ConflictError):
            gaps.resolve(world.run_id, saved["id"], OWNER, **{**args, **override})
    with pytest.raises(ValueError):
        gaps.resolve(world.run_id, saved["id"], OWNER, **{**args, "confirms_answer": False})
    with pytest.raises(NotFoundError):
        gaps.resolve(world.run_id, saved["id"], OWNER, **{**args, "entry_id": uuid4()})
    with pytest.raises(ConflictError):
        gaps.resolve(world.run_id, saved["id"], OWNER, **{**args, "entry_id": world.gateway})
    assert gaps.receipt(saved["id"], READER)["status"] == "pending"


async def test_request_resolution_rejects_other_run_and_accepts_current_maintainer(
    scratch: Scratch,
) -> None:
    world, other = await gap_world(scratch), await gap_world(scratch)
    gaps, books = GapService(scratch.dsn), RunbookService(scratch.dsn)
    saved, _ = gaps.save_request(world.run_id, READER, request_id=uuid4(), question="Who signs?")
    approved = books.get(world.billing)
    args: dict[str, Any] = {
        "expected_revision": saved["revision"],
        "entry_id": world.billing,
        "expected_version": approved.version,
        "confirms_answer": True,
    }
    with pytest.raises(NotFoundError):
        gaps.resolve(world.run_id, saved["id"], OWNER, **{**args, "entry_id": other.billing})
    successor = Principal("samir", "Samir")
    before_review = books.metadata(world.billing)["review_by"]
    with scratch.connect() as conn:
        maintenance.transfer(
            conn,
            world.run_id,
            successor.subject,
            ADMIN,
            expected_owner=OWNER.subject,
            expected_revision=1,
            reason="Expert left",
            start=True,
            identities=(successor,),
        )
    with pytest.raises(NotFoundError):
        gaps.resolve(world.run_id, saved["id"], OWNER, **args)
    resolved = gaps.resolve(world.run_id, saved["id"], successor, **args)
    assert resolved["resolved_by_subject"] == successor.subject
    assert books.metadata(world.billing)["review_by"] == before_review
    assert books.get(world.billing) == approved


async def test_parallel_approval_and_request_resolution_never_link_an_unapproved_version(
    scratch: Scratch,
) -> None:
    world = await gap_world(scratch)
    books, gaps = RunbookService(scratch.dsn), GapService(scratch.dsn)
    with acting_as(OWNER):
        draft = books.edit(
            world.billing, OWNER.name, {"contacts": SectionEdit(claims=("Finance.",))}
        )
    saved, _ = gaps.save_request(world.run_id, READER, request_id=uuid4(), question="Who signs?")
    barrier = Barrier(2)
    results: list[Any] = []

    def approve() -> None:
        with acting_as(OWNER):
            barrier.wait()
            results.append(books.approve(world.billing, OWNER.name, expected_version=draft.version))

    def resolve() -> None:
        barrier.wait()
        try:
            results.append(
                gaps.resolve(
                    world.run_id,
                    saved["id"],
                    OWNER,
                    expected_revision=saved["revision"],
                    entry_id=world.billing,
                    expected_version=draft.version + 1,
                    confirms_answer=True,
                )
            )
        except ConflictError:
            results.append("conflict")

    threads = [Thread(target=approve), Thread(target=resolve)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive(), "run/session/entry/gap lock order must not deadlock"
    assert len(results) == 2
    approved = books.get(world.billing)
    assert approved.entry.status == "approved" and approved.version == draft.version + 1
    final = gaps.resolve(
        world.run_id,
        saved["id"],
        OWNER,
        expected_revision=saved["revision"],
        entry_id=world.billing,
        expected_version=approved.version,
        confirms_answer=True,
    )
    assert final["resolved_version"] == approved.version
    assert len(audit_details(scratch, "gap_resolved")) == 1


async def test_resolution_and_approval_roll_back_when_audit_fails(
    scratch: Scratch, monkeypatch: pytest.MonkeyPatch
) -> None:
    done = await answered_follow_up(scratch)
    books, gaps = RunbookService(scratch.dsn), GapService(scratch.dsn)
    applied = apply_all(books, done)
    original = append_event

    def fail(*args: Any, **kwargs: Any) -> None:
        raise AuditError()

    monkeypatch.setattr(gap_module, "append_event", fail)
    with acting_as(OWNER), pytest.raises(AuditError):
        books.approve(done.world.billing, OWNER.name)
    assert books.get(done.world.billing) == applied
    assert queue(scratch, done.world.run_id)[done.rollback_gap]["status"] == "awaiting_review"
    monkeypatch.setattr(gap_module, "append_event", original)
    with acting_as(OWNER):
        approved = books.approve(done.world.billing, OWNER.name)
    pending = gaps.receipt(done.request_gap, OWNER)
    monkeypatch.setattr(gap_module, "append_event", fail)
    with pytest.raises(AuditError):
        gaps.resolve(
            done.world.run_id,
            done.request_gap,
            OWNER,
            expected_revision=pending["revision"],
            entry_id=done.world.billing,
            expected_version=approved.version,
            confirms_answer=True,
        )
    assert gaps.receipt(done.request_gap, OWNER) == pending


async def test_http_resolution_and_linked_source_expansion_preserve_reader_privacy(
    scratch: Scratch,
) -> None:
    done = await answered_follow_up(scratch)
    books = RunbookService(scratch.dsn)
    apply_all(books, done)
    with acting_as(OWNER):
        approved = books.approve(done.world.billing, OWNER.name)
    client = TestClient(
        create_app(make_service(scratch.dsn), runbook_service=books, authenticator=authenticator())
    )
    source_url = f"/sessions/{done.session_id}"
    source = client.get(source_url, headers=headers("owner"))
    assert source.status_code == 200 and ROLLBACK_ANSWER in source.text
    assert client.get(source_url, headers=headers("reader")).status_code == 404
    public = client.get(f"/entries/{done.world.billing}", headers=headers("reader"))
    assert public.status_code == 200
    assert str(done.session_id) in public.text and "turns" not in public.json()
    assert "unsupported" not in public.json() and "decisions" not in public.json()
    saved = GapService(scratch.dsn).receipt(done.request_gap, OWNER)
    url = f"/runs/{done.world.run_id}/follow-up-gaps/{done.request_gap}/resolve"
    payload = {
        "expected_revision": saved["revision"],
        "entry_id": str(done.world.billing),
        "expected_version": approved.version,
        "confirms_answer": True,
    }
    assert client.post(url, json=payload).status_code == 401
    assert client.post(url, headers=headers("reader"), json=payload).status_code == 404
    assert (
        client.post(
            url, headers=headers("owner"), json={**payload, "confirms_answer": False}
        ).status_code
        == 422
    )
    resolved = client.post(url, headers=headers("owner"), json=payload)
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["status"] == "resolved"
    with scratch.connect() as conn:
        access.set_readers(conn, done.world.run_id, [], OWNER)
    assert (
        client.get(f"/follow-up-requests/{done.request_gap}", headers=headers("reader")).status_code
        == 404
    )
    assert (
        client.get(f"/entries/{done.world.billing}", headers=headers("reader")).status_code == 404
    )
