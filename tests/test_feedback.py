"""F21 reader reports never become publication authority or leak private triage."""

from typing import Any
from uuid import uuid4

import pytest

from eki.auth import acting_as
from eki.errors import ConflictError, NotFoundError
from eki.feedback import FeedbackService
from eki.runbook.service import RunbookService
from tests.conftest import Scratch
from tests.search_support import READER, STRANGER
from tests.test_maintenance import SUCCESSOR, cas, handover

pytestmark = [pytest.mark.db, pytest.mark.anyio]


async def test_report_retry_is_idempotent_and_receipt_is_private(scratch: Scratch) -> None:
    _, entry = await handover(scratch)
    books = RunbookService(scratch.dsn)
    flags = FeedbackService(scratch.dsn)
    request = uuid4()
    args: dict[str, Any] = dict(
        request_id=request,
        expected_version=books.get(entry).version,
        category="outdated",
        section="steps",
        description="FAKE-APIKEY-7d1e4c90b2a853f6 old command",
    )
    first, created = flags.create(entry, READER, **args)
    second, repeated = flags.create(entry, READER, **args)
    assert created and not repeated and first == second
    assert "FAKE-APIKEY" not in str(first)
    with pytest.raises(ConflictError):
        flags.create(entry, READER, **{**args, "description": "Different request"})
    with pytest.raises(NotFoundError):
        flags.get(first["id"], STRANGER)
    assert books.metadata(entry)["open_flag_count"] == 1
    with acting_as(SUCCESSOR):
        books.withdraw(entry, SUCCESSOR.name, "Checking report", **cas(books, entry))
    assert flags.get(first["id"], READER)["resolution"] is None


async def test_confirmed_requires_a_new_review_and_keeps_resolution_notes_private(
    scratch: Scratch,
) -> None:
    _, entry = await handover(scratch)
    books = RunbookService(scratch.dsn)
    flags = FeedbackService(scratch.dsn)
    flag, _ = flags.create(
        entry,
        READER,
        request_id=uuid4(),
        expected_version=books.get(entry).version,
        category="unclear",
        description="Please check",
    )
    with acting_as(SUCCESSOR):
        with pytest.raises(ConflictError):
            flags.resolve(
                flag["id"],
                SUCCESSOR,
                resolution="confirmed_current",
                note="Still correct",
                **cas(books, entry),
            )
        books.review(entry, SUCCESSOR.name, **cas(books, entry))
        flags.resolve(
            flag["id"],
            SUCCESSOR,
            resolution="confirmed_current",
            note="PRIVATE OWNER NOTES",
            **cas(books, entry),
        )
    assert "PRIVATE OWNER" not in str(flags.get(flag["id"], READER))
    assert books.metadata(entry)["open_flag_count"] == 0
    with acting_as(SUCCESSOR), pytest.raises(ConflictError):
        flags.resolve(
            flag["id"], SUCCESSOR, resolution="dismissed", note="Again", **cas(books, entry)
        )


async def test_rate_limit_counts_new_reports_not_replays(scratch: Scratch) -> None:
    from eki.accounts import Throttled

    _, entry = await handover(scratch)
    books = RunbookService(scratch.dsn)
    flags = FeedbackService(scratch.dsn)
    args: dict[str, Any] = dict(
        request_id=uuid4(),
        expected_version=books.get(entry).version,
        category="unclear",
        description="Question",
    )
    flags.create(entry, READER, **args)
    for _ in range(12):
        flags.create(entry, READER, **args)
    for _ in range(9):
        flags.create(entry, READER, **{**args, "request_id": uuid4()})
    with pytest.raises(Throttled):
        flags.create(entry, READER, **{**args, "request_id": uuid4()})


async def test_http_reader_receipt_and_owner_review_resolution(scratch: Scratch) -> None:
    import json

    from fastapi.testclient import TestClient

    from eki.api.app import create_app
    from eki.config import Settings

    _, entry = await handover(scratch)
    books = RunbookService(scratch.dsn)
    token = "feedback-test-token-0000000000000001"
    owner_token = "owner-test-token-0000000000000000001"
    settings = Settings(
        _env_file=None,
        database_url=scratch.dsn,
        api_tokens=json.dumps(
            [
                {"token": token, "subject": READER.subject, "name": READER.name},
                {"token": owner_token, "subject": SUCCESSOR.subject, "name": SUCCESSOR.name},
            ]
        ),
    )
    client = TestClient(create_app(settings=settings, runbook_service=books))
    reader = {"Authorization": "Bearer " + token}
    owner = {"Authorization": "Bearer " + owner_token}
    data = {
        "request_id": str(uuid4()),
        "expected_version": books.get(entry).version,
        "category": "outdated",
        "description": "Please check command",
    }
    saved = client.post(f"/entries/{entry}/flags", headers=reader, json=data)
    assert saved.status_code == 201, saved.text
    assert client.post(f"/entries/{entry}/flags", headers=reader, json=data).status_code == 200
    assert client.get(f"/entries/{entry}/flags", headers=reader).status_code == 404
    review = client.post(f"/entries/{entry}/review", headers=owner, json=cas(books, entry))
    assert review.status_code == 200, review.text
    resolved = client.post(
        f"/entries/{entry}/flags/{saved.json()['id']}/resolve",
        headers=owner,
        json={
            **cas(books, entry),
            "resolution": "confirmed_current",
            "note": "Private triage note",
        },
    )
    assert resolved.status_code == 200, resolved.text
    receipt = client.get(f"/flags/{saved.json()['id']}", headers=reader)
    assert receipt.status_code == 200 and "Private triage" not in receipt.text
    assert receipt.json()["resolution"] == "confirmed_current"


async def test_audit_failure_rolls_back_report_and_throttle(
    scratch: Scratch, monkeypatch: pytest.MonkeyPatch
) -> None:
    from eki import feedback
    from eki.audit import AuditError

    _, entry = await handover(scratch)
    books = RunbookService(scratch.dsn)
    before = books.metadata(entry)

    def fail(*args: Any, **kwargs: Any) -> None:
        raise AuditError("unavailable")

    monkeypatch.setattr(feedback, "append_event", fail)
    with pytest.raises(AuditError):
        FeedbackService(scratch.dsn).create(
            entry,
            READER,
            request_id=uuid4(),
            expected_version=books.get(entry).version,
            category="unclear",
            description="Question",
        )
    assert books.metadata(entry) == before
    with scratch.connect() as conn:
        assert conn.execute("SELECT count(*) FROM flag_throttle").fetchone() == (0,)
