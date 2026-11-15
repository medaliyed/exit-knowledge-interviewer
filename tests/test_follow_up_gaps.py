"""F22 9a: public open sections and explicitly saved Ask questions become a triaged queue."""

import json
from typing import Any
from uuid import UUID, uuid4

import pytest

from eki import access, maintenance
from eki.auth import Principal, acting_as
from eki.errors import ConflictError, NotFoundError
from eki.followup.gaps import GapService, open_section_gap_id
from eki.runbook.review import SectionEdit
from eki.runbook.service import RunbookService
from tests.conftest import Scratch
from tests.followup_support import OPEN_REASON, gap_world
from tests.search_support import ADMIN, OWNER, READER, STRANGER

pytestmark = [pytest.mark.db, pytest.mark.anyio]
OTHER_READER = Principal("other", "Other Reader")


def gaps(scratch: Scratch) -> GapService:
    return GapService(scratch.dsn)


def request(
    service: GapService, run: UUID, who: Principal = READER, **overrides: Any
) -> tuple[dict[str, Any], bool]:
    args: dict[str, Any] = {
        "request_id": uuid4(),
        "question": "Who authorises reopening a closed period?",
        **overrides,
    }
    return service.save_request(run, who, **args)


# --- G1 / A1: candidates are current public open sections only ---


async def test_queue_lists_only_current_public_open_sections(scratch: Scratch) -> None:
    world = await gap_world(scratch)
    service = gaps(scratch)
    listed = service.queue(world.run_id, OWNER)["gaps"]
    assert [(g["kind"], g["entry_id"], g["section"]) for g in listed] == [
        ("open_section", str(world.billing), "rollback")
    ]
    gap = listed[0]
    assert gap["id"] == str(open_section_gap_id(world.billing, "rollback"))
    assert gap["open_question"] == OPEN_REASON
    assert gap["status"] == "pending"
    assert gap["entry_status"] == "approved"
    # Filling the section in a later revision removes it; history is not re-added.
    books = RunbookService(scratch.dsn)
    books.edit(world.billing, OWNER.name, {"rollback": SectionEdit(claims=("Restore export.",))})
    assert service.queue(world.run_id, OWNER)["gaps"] == []


async def test_rejected_entries_and_readers_are_outside_the_queue(scratch: Scratch) -> None:
    world = await gap_world(scratch)
    books = RunbookService(scratch.dsn)
    books.edit(world.billing, OWNER.name, {"steps": SectionEdit(claims=("Changed step.",))})
    assert gaps(scratch).queue(world.run_id, OWNER)["gaps"], "an edited draft keeps its gap"
    books.reject(world.billing, OWNER.name, "Outdated")
    service = gaps(scratch)
    assert service.queue(world.run_id, OWNER)["gaps"] == []
    assert service.queue(world.run_id, ADMIN)["gaps"] == []
    with pytest.raises(NotFoundError):
        service.queue(world.run_id, READER)


# --- C3 / A2: Ask explains why it abstained and stores nothing by default ---


async def test_ask_reports_a_safe_abstention_reason_and_writes_no_history(
    scratch: Scratch, monkeypatch: pytest.MonkeyPatch
) -> None:
    from eki.search import answers as module
    from eki.search.answers import AnswerService
    from eki.search.service import SearchService

    world = await gap_world(scratch)
    service = AnswerService(SearchService(scratch.dsn))
    missing = await service.ask(READER, "How do I renew the vendor certificate for payroll?")
    assert missing.status == "insufficient_evidence"
    assert missing.reason == "no_supported_answer"
    monkeypatch.setattr(AnswerService, "_current", lambda self, principal, hits: False)
    changed = await service.ask(READER, "Tell me about incident:billing:stale trigger")
    assert changed.reason == "evidence_changed" and not changed.open_questions
    monkeypatch.undo()
    original = module._candidates

    def truncated(hits: Any, question: str) -> Any:
        ranked = original(hits, question)
        return [item for item in ranked if item[1].claim_index == 0][:1]

    monkeypatch.setattr(module, "_candidates", truncated)
    with scratch.connect() as conn:
        conn.execute(
            "UPDATE runbook_entries SET content=jsonb_set(content,'{steps,claims}',"
            "(content->'steps'->'claims') || (content->'steps'->'claims')) WHERE id=%s",
            (world.ledger,),
        )
    limited = await service.ask(READER, "What are the steps for incident:ledger:drift?")
    assert limited.status == "insufficient_evidence"
    assert limited.reason == "context_limit"
    with scratch.connect() as conn:
        assert conn.execute("SELECT count(*) FROM follow_up_gaps").fetchone() == (0,)


# --- G2 / G3 / A3: explicit, idempotent, redacted saves to a permitted run ---


async def test_save_request_is_explicit_idempotent_and_redacted(scratch: Scratch) -> None:
    world = await gap_world(scratch)
    service = gaps(scratch)
    request_id = uuid4()
    first, created = request(
        service,
        world.run_id,
        request_id=request_id,
        question="Who signs off? Token FAKE-APIKEY-7d1e4c90b2a853f6 is in the ticket",
        entry_id=world.billing,
        section="contacts",
    )
    assert created and first["status"] == "pending" and first["kind"] == "request"
    assert "FAKE-APIKEY" not in json.dumps(first)
    again, repeated = request(
        service,
        world.run_id,
        request_id=request_id,
        question="Who signs off? Token FAKE-APIKEY-7d1e4c90b2a853f6 is in the ticket",
        entry_id=world.billing,
        section="contacts",
    )
    assert not repeated and again == first
    with pytest.raises(ConflictError):
        request(service, world.run_id, request_id=request_id, question="Different text")
    collapsed, fresh = request(service, world.run_id, question=first["question"])
    assert not fresh and collapsed["id"] == first["id"], "same text by the same reader collapses"
    with scratch.connect() as conn:
        access.set_readers(conn, world.run_id, [READER.subject, OTHER_READER.subject], OWNER)
    other, _ = request(service, world.run_id, who=OTHER_READER, question=first["question"])
    assert other["id"] != first["id"], "another reader's identical question is their own"
    for bad in (
        {"question": " "},
        {"question": "x" * 2001},
        {"entry_id": world.gateway},
        {"entry_id": world.billing, "section": "appendix"},
    ):
        with pytest.raises((ValueError, NotFoundError)):
            request(service, world.run_id, **bad)
    with pytest.raises(NotFoundError):
        request(service, world.run_id, who=STRANGER)
    with pytest.raises(NotFoundError):
        request(service, uuid4())


async def test_requests_reference_only_entries_of_the_chosen_run(scratch: Scratch) -> None:
    from tests.search_support import search_world

    world = await gap_world(scratch)
    elsewhere = await search_world(scratch)
    with pytest.raises(NotFoundError):
        request(gaps(scratch), world.run_id, entry_id=elsewhere.billing)


# --- G4 / A3: receipts are private and rate limited ---


async def test_receipt_is_private_and_revoked_with_readership(scratch: Scratch) -> None:
    world = await gap_world(scratch)
    service = gaps(scratch)
    saved, _ = request(service, world.run_id)
    mine = service.receipt(saved["id"], READER)
    assert mine["status"] == "pending" and "requester_subject" not in mine
    with pytest.raises(NotFoundError):
        service.receipt(saved["id"], OTHER_READER)
    with pytest.raises(NotFoundError):
        service.receipt(saved["id"], STRANGER)
    assert service.receipt(saved["id"], OWNER)["requester_subject"] == READER.subject
    listed = service.queue(world.run_id, OWNER)["gaps"]
    assert any(g["id"] == saved["id"] and g["requester_subject"] == READER.subject for g in listed)
    with scratch.connect() as conn:
        access.set_readers(conn, world.run_id, [OTHER_READER.subject], OWNER)
    with pytest.raises(NotFoundError):
        service.receipt(saved["id"], READER)


async def test_rate_limit_counts_new_requests_not_replays(scratch: Scratch) -> None:
    from eki.accounts import Throttled

    world = await gap_world(scratch)
    service = gaps(scratch)
    request_id = uuid4()
    for _ in range(5):
        request(service, world.run_id, request_id=request_id)
    for n in range(9):
        request(service, world.run_id, question=f"Question {n}")
    with pytest.raises(Throttled):
        request(service, world.run_id, question="One too many")


# --- G5 / G6: owner triage with optimistic revisions ---


async def test_triage_maps_defers_dismisses_and_checks_revisions(scratch: Scratch) -> None:
    world = await gap_world(scratch)
    service = gaps(scratch)
    saved, _ = request(service, world.run_id)
    mapped = service.triage(
        world.run_id,
        saved["id"],
        OWNER,
        action="map",
        expected_revision=saved["revision"],
        target_entry_id=world.billing,
        target_section="contacts",
        seed_question="Who can authorise reopening a closed period? Ping nadia@example.com",
        priority=2,
    )
    assert mapped["target_entry_id"] == str(world.billing)
    assert mapped["target_section"] == "contacts" and mapped["priority"] == 2
    assert "nadia@example.com" not in mapped["seed_question"]
    assert mapped["status"] == "pending", "mapping is triage, not scheduling"
    with pytest.raises(ConflictError):
        service.triage(
            world.run_id,
            saved["id"],
            OWNER,
            action="defer",
            expected_revision=saved["revision"],
            reason="Later",
        )
    with pytest.raises(ValueError):
        service.triage(
            world.run_id,
            saved["id"],
            OWNER,
            action="dismiss",
            expected_revision=mapped["revision"],
            reason="  ",
        )
    deferred = service.triage(
        world.run_id,
        saved["id"],
        OWNER,
        action="defer",
        expected_revision=mapped["revision"],
        reason="Wait for the audit",
    )
    assert deferred["status"] == "deferred"
    assert service.receipt(saved["id"], READER)["status"] == "deferred"
    assert "reason" not in service.receipt(saved["id"], READER)
    dismissed = service.triage(
        world.run_id,
        saved["id"],
        OWNER,
        action="dismiss",
        expected_revision=deferred["revision"],
        reason="Duplicate of the rollback gap",
    )
    assert [g["id"] for g in service.queue(world.run_id, OWNER)["gaps"]] == [
        str(open_section_gap_id(world.billing, "rollback"))
    ]
    hidden = service.queue(world.run_id, OWNER, filter="dismissed")["gaps"]
    assert [g["id"] for g in hidden] == [saved["id"]]
    reopened = service.triage(
        world.run_id,
        saved["id"],
        OWNER,
        action="reopen",
        expected_revision=dismissed["revision"],
    )
    assert reopened["status"] == "pending"


async def test_open_section_triage_and_new_topic_choice(scratch: Scratch) -> None:
    world = await gap_world(scratch)
    service = gaps(scratch)
    gap_id = str(open_section_gap_id(world.billing, "rollback"))
    (listed,) = service.queue(world.run_id, OWNER)["gaps"]
    dismissed = service.triage(
        world.run_id,
        gap_id,
        OWNER,
        action="dismiss",
        expected_revision=listed["revision"],
        reason="Rollback is impossible by design",
    )
    assert dismissed["status"] == "dismissed"
    assert service.queue(world.run_id, OWNER)["gaps"] == []
    saved, _ = request(service, world.run_id)
    topic = service.triage(
        world.run_id,
        saved["id"],
        OWNER,
        action="new_topic",
        expected_revision=saved["revision"],
        topic_label="Period reopening approvals",
        seed_question="Who can authorise reopening a closed period?",
    )
    assert topic["new_topic"] and topic["topic_label"] == "Period reopening approvals"
    assert topic["target_entry_id"] is None


async def test_only_managers_triage_and_rejected_targets_are_refused(scratch: Scratch) -> None:
    world = await gap_world(scratch)
    service = gaps(scratch)
    saved, _ = request(service, world.run_id)
    for who in (READER, STRANGER, Principal("impostor", OWNER.name)):
        with pytest.raises(NotFoundError):
            service.triage(
                world.run_id,
                saved["id"],
                who,
                action="defer",
                expected_revision=saved["revision"],
                reason="No",
            )
    books = RunbookService(scratch.dsn)
    books.edit(world.ledger, OWNER.name, {"steps": SectionEdit(claims=("Changed step.",))})
    books.reject(world.ledger, OWNER.name, "Outdated")
    with pytest.raises(ConflictError):
        service.triage(
            world.run_id,
            saved["id"],
            OWNER,
            action="map",
            expected_revision=saved["revision"],
            target_entry_id=world.ledger,
            target_section="steps",
        )
    with pytest.raises(NotFoundError):
        service.triage(
            uuid4(),
            saved["id"],
            ADMIN,
            action="defer",
            expected_revision=saved["revision"],
            reason="Wrong run",
        )


async def test_successor_triages_after_handover(scratch: Scratch) -> None:
    successor = Principal("samir", "Samir")
    world = await gap_world(scratch)
    service = gaps(scratch)
    saved, _ = request(service, world.run_id)
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
    deferred = service.triage(
        world.run_id,
        saved["id"],
        successor,
        action="defer",
        expected_revision=saved["revision"],
        reason="Successor will decide",
    )
    assert deferred["status"] == "deferred"
    with pytest.raises(NotFoundError):
        service.triage(
            world.run_id,
            saved["id"],
            OWNER,
            action="reopen",
            expected_revision=deferred["revision"],
        )


# --- C2 / A12: audit commits with the change and holds no bodies ---


async def test_audit_failure_rolls_back_request_and_bodies_stay_out_of_audit(
    scratch: Scratch, monkeypatch: pytest.MonkeyPatch
) -> None:
    from eki.audit import AuditError, list_events
    from eki.followup import gaps as module

    world = await gap_world(scratch)
    service = gaps(scratch)
    _saved, _ = request(service, world.run_id, question="Secret-ish wording here")
    with scratch.connect() as conn:
        events = list_events(conn, world.run_id)
    assert any("follow_up_requested" in json.dumps(e["details"]) for e in events)
    assert "Secret-ish" not in json.dumps(events)
    monkeypatch.setattr(module, "append_event", lambda *a, **k: (_ for _ in ()).throw(AuditError()))
    with pytest.raises(AuditError):
        request(service, world.run_id, question="Never stored")
    with scratch.connect() as conn:
        assert conn.execute("SELECT count(*) FROM follow_up_gaps").fetchone() == (1,)
        assert conn.execute("SELECT count FROM follow_up_throttle").fetchone() == (1,)


# --- C4 / A3: HTTP surfaces ---


async def test_http_reader_save_receipt_and_owner_queue(scratch: Scratch) -> None:
    from fastapi.testclient import TestClient

    from eki.api.app import create_app
    from eki.config import Settings

    world = await gap_world(scratch)
    books = RunbookService(scratch.dsn)
    reader_token = "gap-reader-token-000000000000000000001"
    owner_token = "gap-owner-token-0000000000000000000001"
    settings = Settings(
        _env_file=None,
        database_url=scratch.dsn,
        api_tokens=json.dumps(
            [
                {"token": reader_token, "subject": READER.subject, "name": READER.name},
                {"token": owner_token, "subject": OWNER.subject, "name": OWNER.name},
            ]
        ),
    )
    client = TestClient(create_app(settings=settings, runbook_service=books))
    reader = {"Authorization": "Bearer " + reader_token}
    owner = {"Authorization": "Bearer " + owner_token}
    body = {"request_id": str(uuid4()), "question": "Who reopens a closed period?"}
    saved = client.post(f"/runs/{world.run_id}/follow-up-requests", headers=reader, json=body)
    assert saved.status_code == 201, saved.text
    assert (
        client.post(
            f"/runs/{world.run_id}/follow-up-requests", headers=reader, json=body
        ).status_code
        == 200
    )
    assert (
        client.post(
            f"/runs/{world.run_id}/follow-up-requests",
            headers=reader,
            json={**body, "question": "Changed"},
        ).status_code
        == 409
    )
    assert (
        client.post(
            f"/runs/{world.run_id}/follow-up-requests",
            headers=reader,
            json={**body, "request_id": str(uuid4()), "question": "x" * 2001},
        ).status_code
        == 422
    )
    gap_id = saved.json()["id"]
    receipt = client.get(f"/follow-up-requests/{gap_id}", headers=reader)
    assert receipt.status_code == 200 and "requester_subject" not in receipt.json()
    assert client.get(f"/runs/{world.run_id}/follow-up-gaps", headers=reader).status_code == 404
    queue = client.get(f"/runs/{world.run_id}/follow-up-gaps", headers=owner)
    assert queue.status_code == 200 and len(queue.json()["gaps"]) == 2
    triaged = client.patch(
        f"/runs/{world.run_id}/follow-up-gaps/{gap_id}",
        headers=owner,
        json={
            "action": "map",
            "expected_revision": saved.json()["revision"],
            "target_entry_id": str(world.billing),
            "target_section": "contacts",
        },
    )
    assert triaged.status_code == 200, triaged.text
    assert (
        client.patch(
            f"/runs/{world.run_id}/follow-up-gaps/{gap_id}",
            headers=reader,
            json={"action": "defer", "expected_revision": 1, "reason": "No"},
        ).status_code
        == 404
    )
    stale = client.patch(
        f"/runs/{world.run_id}/follow-up-gaps/{gap_id}",
        headers=owner,
        json={"action": "defer", "expected_revision": saved.json()["revision"], "reason": "x"},
    )
    assert stale.status_code == 409
    with acting_as(OWNER):
        answer = client.post(
            "/runbook/ask", headers=reader, json={"question": "How do I renew payroll certs?"}
        )
    assert answer.json()["reason"] == "no_supported_answer"
