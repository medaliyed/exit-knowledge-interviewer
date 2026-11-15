"""F21 acceptance gaps: unassigned handover, CLI authority, payload bounds, revocation,
interleaved writes, strict corrected resolutions and queue/summary clocks."""

import asyncio
import json
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from typing import Any
from uuid import UUID, uuid4

import pytest

from eki import access, maintenance
from eki.auth import Principal, acting_as
from eki.errors import ConflictError, NotFoundError
from eki.feedback import FeedbackService
from eki.runbook.review import SectionEdit
from eki.runbook.service import RunbookService
from tests.conftest import Scratch
from tests.search_support import ADMIN, OWNER, READER, search_world
from tests.test_maintenance import SUCCESSOR, cas, handover

pytestmark = [pytest.mark.db]


def flag(books: RunbookService, entry: UUID, **overrides: Any) -> dict[str, Any]:
    args: dict[str, Any] = {
        "request_id": uuid4(),
        "expected_version": books.get(entry).version,
        "category": "outdated",
        "description": "The command is obsolete",
        **overrides,
    }
    created, _ = FeedbackService(books.dsn).create(entry, READER, **args)
    return created


# --- A12/C7: an unassigned legacy run enters maintenance directly ---


def test_unassigned_run_can_be_handed_over(scratch: Scratch) -> None:
    world = asyncio.run(search_world(scratch))
    with scratch.connect() as conn:
        conn.execute(
            "UPDATE ingest_runs SET owner_subject=NULL,maintenance_revision=0 WHERE id=%s",
            (world.run_id,),
        )
        with pytest.raises(ConflictError):
            maintenance.transfer(
                conn,
                world.run_id,
                SUCCESSOR.subject,
                ADMIN,
                expected_owner=OWNER.subject,
                expected_revision=0,
                reason="Expert left",
                start=True,
                identities=(SUCCESSOR,),
            )
        state = maintenance.transfer(
            conn,
            world.run_id,
            SUCCESSOR.subject,
            ADMIN,
            expected_owner=None,
            expected_revision=0,
            reason="Expert left",
            start=True,
            identities=(SUCCESSOR,),
        )
    assert state["owner_subject"] == SUCCESSOR.subject
    assert state["maintenance_started_at"] is not None
    assert state["history"][-1]["previous_owner"] is None
    assert state["history"][-1]["started_maintenance"] is True


# --- A8: invalid and oversized reports are refused before storage ---


def test_invalid_or_oversized_reports_are_refused(scratch: Scratch) -> None:
    _, entry = asyncio.run(handover(scratch))
    books = RunbookService(scratch.dsn)
    for bad in (
        {"description": "x" * 2001},
        {"description": "   "},
        {"category": "spam"},
        {"section": "appendix"},
    ):
        with pytest.raises(ValueError):
            flag(books, entry, **bad)
    with scratch.connect() as conn:
        assert conn.execute("SELECT count(*) FROM entry_flags").fetchone() == (0,)


def test_http_rejects_oversized_report(scratch: Scratch) -> None:
    from fastapi.testclient import TestClient

    from eki.api.app import create_app
    from eki.config import Settings

    _, entry = asyncio.run(handover(scratch))
    books = RunbookService(scratch.dsn)
    token = "gap-reader-token-00000000000000000001"
    settings = Settings(
        _env_file=None,
        database_url=scratch.dsn,
        api_tokens=json.dumps([{"token": token, "subject": READER.subject, "name": READER.name}]),
    )
    client = TestClient(create_app(settings=settings, runbook_service=books))
    response = client.post(
        f"/entries/{entry}/flags",
        headers={"Authorization": "Bearer " + token},
        json={
            "request_id": str(uuid4()),
            "expected_version": books.get(entry).version,
            "category": "outdated",
            "description": "x" * 2001,
        },
    )
    assert response.status_code == 422
    assert "x" * 50 not in response.text


# --- A9: revoking readership revokes the reporter's receipt ---


def test_revoked_reader_loses_receipt_access(scratch: Scratch) -> None:
    run, entry = asyncio.run(handover(scratch))
    books = RunbookService(scratch.dsn)
    created = flag(books, entry)
    flags = FeedbackService(scratch.dsn)
    assert flags.get(created["id"], READER)["id"] == created["id"]
    with scratch.connect() as conn:
        access.set_readers(conn, run, [], SUCCESSOR)
    with pytest.raises(NotFoundError):
        flags.get(created["id"], READER)


# --- A10: "corrected" needs a content change after the report ---


def test_corrected_requires_a_content_edit_after_the_report(scratch: Scratch) -> None:
    _, entry = asyncio.run(handover(scratch))
    books = RunbookService(scratch.dsn)
    flags = FeedbackService(scratch.dsn)
    created = flag(books, entry)
    with acting_as(SUCCESSOR):
        books.withdraw(entry, SUCCESSOR.name, "Checking", **cas(books, entry))
        books.approve(entry, SUCCESSOR.name, **cas(books, entry))
        with pytest.raises(ConflictError, match="edit"):
            flags.resolve(
                created["id"],
                SUCCESSOR,
                resolution="corrected",
                note="Nothing changed",
                **cas(books, entry),
            )
        books.edit(
            entry,
            SUCCESSOR.name,
            {"steps": SectionEdit(claims=("Use the replacement command.",))},
            **cas(books, entry),
        )
        books.approve(entry, SUCCESSOR.name, **cas(books, entry))
        result = flags.resolve(
            created["id"],
            SUCCESSOR,
            resolution="corrected",
            note="Steps updated",
            **cas(books, entry),
        )
    assert result["resolution"] == "corrected"


# --- A11: interleaved writes on the same expected revision commit exactly once ---


def race(*calls: Callable[[], object]) -> list[BaseException | None]:
    start = Barrier(len(calls))

    def run(call: Callable[[], object]) -> BaseException | None:
        start.wait(timeout=5)
        try:
            with acting_as(SUCCESSOR):
                call()
        except (ConflictError, NotFoundError) as error:
            return error
        return None

    with ThreadPoolExecutor(max_workers=len(calls)) as pool:
        futures = [pool.submit(run, call) for call in calls]
        return [f.result(timeout=20) for f in futures]


def test_review_and_withdraw_race_commits_one_change(scratch: Scratch) -> None:
    _, entry = asyncio.run(handover(scratch))
    books = RunbookService(scratch.dsn)
    args = cas(books, entry)
    outcomes = race(
        lambda: books.review(entry, SUCCESSOR.name, **args),
        lambda: books.withdraw(entry, SUCCESSOR.name, "Suspect", **args),
    )
    assert sum(o is None for o in outcomes) == 1
    assert all(isinstance(o, ConflictError) for o in outcomes if o is not None)
    with scratch.connect() as conn:
        actions = conn.execute(
            "SELECT action FROM entry_decisions WHERE entry_id=%s AND action IN"
            " ('review','withdraw')",
            (entry,),
        ).fetchall()
    assert len(actions) == 1


def test_concurrent_flag_resolutions_commit_once(scratch: Scratch) -> None:
    _, entry = asyncio.run(handover(scratch))
    books = RunbookService(scratch.dsn)
    flags = FeedbackService(scratch.dsn)
    created = flag(books, entry)
    args = cas(books, entry)
    outcomes = race(
        lambda: flags.resolve(
            created["id"], SUCCESSOR, resolution="dismissed", note="Duplicate", **args
        ),
        lambda: flags.resolve(
            created["id"], SUCCESSOR, resolution="dismissed", note="Unrelated", **args
        ),
    )
    assert sum(o is None for o in outcomes) == 1
    assert books.metadata(entry)["open_flag_count"] == 0


# --- R7/D4: queue order and summary counts use live approval and the injected clock ---


def test_withdrawn_draft_does_not_sort_by_its_stale_review_date(scratch: Scratch) -> None:
    run, entry = asyncio.run(handover(scratch))
    books = RunbookService(scratch.dsn)
    today = datetime.now(UTC).date()
    with scratch.connect() as conn:
        conn.execute(
            "UPDATE runbook_entries SET review_by=%s WHERE id=%s",
            (today - timedelta(days=5), entry),
        )
    with acting_as(SUCCESSOR):
        books.withdraw(entry, SUCCESSOR.name, "Checking", **cas(books, entry))
    with scratch.connect() as conn:
        rows = maintenance.queue(conn, run, SUCCESSOR)["entries"]
        world_gateway = next(r["id"] for r in rows if r["title"].startswith("Gateway"))
    ids = [r["id"] for r in rows]
    assert ids.index(world_gateway) < ids.index(str(entry))


def test_summary_counts_overdue_with_the_supplied_clock(scratch: Scratch) -> None:
    run, entry = asyncio.run(handover(scratch))
    review_by = RunbookService(scratch.dsn).metadata(entry)["review_by"]
    later = datetime.fromisoformat(review_by).replace(tzinfo=UTC) + timedelta(days=1)
    with scratch.connect() as conn:
        assert maintenance.state(conn, run, SUCCESSOR)["summary"]["overdue"] == 0
        assert maintenance.state(conn, run, SUCCESSOR, now=later)["summary"]["overdue"] >= 1


# --- A2/H5: CLI review authority after handover ---


def cli_identity(monkeypatch: pytest.MonkeyPatch, scratch: Scratch, who: Principal) -> None:
    from eki.config import get_settings

    token = f"cli-{who.subject}-token-000000000000000000001"
    monkeypatch.setenv("EKI_DATABASE_URL", scratch.dsn)
    monkeypatch.setenv("EKI_MODEL_DRAFTER", "")
    monkeypatch.setenv("EKI_MODEL_INTERVIEWER", "")
    monkeypatch.setenv("EKI_API_TOKEN", token)
    monkeypatch.setenv(
        "EKI_API_TOKENS",
        json.dumps(
            [{"token": token, "subject": who.subject, "name": who.name, "admin": who.admin}]
        ),
    )
    get_settings.cache_clear()


def test_cli_review_refuses_everyone_but_the_current_owner(
    scratch: Scratch, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from eki.cli import main
    from eki.config import get_settings
    from eki.interview import store as interviews
    from tests.test_cli import typed

    run, entry = asyncio.run(handover(scratch))
    with scratch.connect() as conn:
        session = interviews.load_session(conn, RunbookService(scratch.dsn).get(entry).session_id)
        assert session is not None
    before = RunbookService(scratch.dsn).list(session.id)
    try:
        for who in (OWNER, ADMIN, Principal("impostor", SUCCESSOR.name)):
            cli_identity(monkeypatch, scratch, who)
            typed(monkeypatch, "approve")
            assert main(["review", "--plan", str(session.plan_id)]) == 1
            assert main(["review", "--run", str(run)]) == 1
        cli_identity(monkeypatch, scratch, OWNER)
        assert main(["review", "--plan", str(session.plan_id), "--review-as", SUCCESSOR.name]) == 1
        assert RunbookService(scratch.dsn).list(session.id) == before
        cli_identity(monkeypatch, scratch, SUCCESSOR)
        typed(monkeypatch, "skip")
        assert main(["review", "--plan", str(session.plan_id)]) == 0
    finally:
        get_settings.cache_clear()
    capsys.readouterr()


def test_cli_due_review_includes_overdue_and_due_soon(
    scratch: Scratch, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from eki.cli import main
    from eki.config import get_settings
    from tests.test_cli import typed

    world = asyncio.run(search_world(scratch))
    with scratch.connect() as conn:
        maintenance.transfer(
            conn,
            world.run_id,
            SUCCESSOR.subject,
            ADMIN,
            expected_owner=OWNER.subject,
            expected_revision=1,
            reason="Expert left",
            start=True,
            identities=(SUCCESSOR,),
        )
        today = datetime.now(UTC).date()
        conn.execute(
            "UPDATE runbook_entries SET review_by=%s WHERE id=%s",
            (today - timedelta(days=1), world.billing),
        )
        conn.execute(
            "UPDATE runbook_entries SET review_by=%s WHERE id=%s",
            (today + timedelta(days=10), world.gateway),
        )
    try:
        cli_identity(monkeypatch, scratch, SUCCESSOR)
        typed(monkeypatch, "skip", "skip", "skip")
        assert main(["review", "--run", str(world.run_id), "--due"]) == 0
    finally:
        get_settings.cache_clear()
    out = capsys.readouterr().out
    assert "Billing month-end reconciliation" in out
    assert "Gateway certificate renewal" in out
    assert "Unapproved payroll procedure" not in out


def test_cli_review_prompt_lists_every_action(
    scratch: Scratch, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from eki.cli import main
    from eki.config import get_settings
    from tests.test_cli import typed

    run, _ = asyncio.run(handover(scratch))
    try:
        cli_identity(monkeypatch, scratch, SUCCESSOR)
        typed(monkeypatch, "nonsense")
        assert main(["review", "--run", str(run)]) == 0
    finally:
        get_settings.cache_clear()
    out = capsys.readouterr().out
    assert (
        "review unchanged, withdraw, reject, edit, triage, skip, or quit"
        in out.split("Choose ")[-1]
    )
