"""F9/F10: batched entry reads describe one committed database snapshot."""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest

from eki import access, pool
from eki.auth import acting_as
from eki.errors import NotFoundError
from eki.followup import proposals
from eki.interview import store as interviews
from eki.runbook import faithfulness, store
from eki.runbook.review import SectionEdit
from eki.runbook.service import RunbookService
from tests.conftest import Scratch
from tests.followup_support import answered_follow_up
from tests.search_support import OWNER, READER, search_world
from tests.test_workflow_synthesis import finished

pytestmark = [pytest.mark.db, pytest.mark.anyio]


async def test_summaries_batch_metadata_and_entry_checks(
    scratch: Scratch, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = await finished(scratch)
    books = RunbookService(scratch.dsn)
    entries = books.list(session)
    expected = books.unsupported(session)
    metadata = {e.entry.id: books.metadata(UUID(e.entry.id)) for e in entries}
    statements: list[str] = []
    borrows: list[str] = []
    real_execute, real_connection = psycopg.Connection.execute, pool.connection

    def execute(self: psycopg.Connection, query: Any, *args: Any, **kwargs: Any) -> Any:
        statements.append(str(query))
        return real_execute(self, query, *args, **kwargs)

    @contextmanager
    def connection(dsn: str) -> Iterator[psycopg.Connection]:
        borrows.append(dsn)
        with real_connection(dsn) as conn:
            yield conn

    monkeypatch.setattr(psycopg.Connection, "execute", execute)
    monkeypatch.setattr(pool, "connection", connection)
    many = books.summaries(session)
    assert [s.stored for s in many] == list(entries)
    assert all(s.unsupported == expected[s.stored.entry.id] for s in many)
    assert all(s.metadata == metadata[s.stored.entry.id] for s in many)
    assert len(borrows) == 1
    many_cost = len(statements)
    with scratch.connect() as conn:
        conn.execute(
            "DELETE FROM runbook_entries WHERE id=ANY(%s::uuid[])",
            ([e.entry.id for e in entries[1:]],),
        )
    statements.clear()
    borrows.clear()
    assert len(books.summaries(session)) == 1
    assert len(statements) == many_cost
    assert len(borrows) == 1


async def test_detail_stays_consistent_when_an_edit_commits_between_reads(
    scratch: Scratch, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = await finished(scratch)
    books = RunbookService(scratch.dsn)
    before = books.list(session)[0]
    entry_id = UUID(before.entry.id)
    metadata = books.metadata(entry_id)
    unsupported = books.unsupported(session)[before.entry.id]
    real_load = store.load_entry
    checked: list[str] = []
    real_check = faithfulness.check_entry

    def check(entry: Any, *args: Any, **kwargs: Any) -> Any:
        checked.append(entry.id)
        return real_check(entry, *args, **kwargs)

    def load(conn: psycopg.Connection, key: str, **kwargs: Any) -> store.StoredEntry | None:
        stored = real_load(conn, key, **kwargs)
        # Restore first: the concurrent writer also loads the entry.
        monkeypatch.setattr(store, "load_entry", real_load)
        books.edit(entry_id, OWNER.name, {"steps": SectionEdit(claims=("Restart twice.",))})
        return stored

    monkeypatch.setattr(store, "load_entry", load)
    monkeypatch.setattr(faithfulness, "check_entry", check)
    detail = books.detail(entry_id)
    assert detail.stored == before
    assert detail.metadata == metadata
    assert detail.unsupported == unsupported
    assert detail.decisions == ()
    assert checked == [before.entry.id]
    current = books.detail(entry_id)
    assert current.stored.version == before.version + 1
    assert current.metadata["maintenance_revision"] > metadata["maintenance_revision"]
    assert [d.action for d in current.decisions] == ["edit"]
    assert not any(u.section == "steps" for u in current.unsupported)


async def test_summaries_keep_pre_edit_metadata_and_evidence(
    scratch: Scratch, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = await finished(scratch)
    books = RunbookService(scratch.dsn)
    before = books.summaries(session)
    real = store.list_entries

    def listing(conn: psycopg.Connection, session_id: UUID) -> tuple[store.StoredEntry, ...]:
        entries = real(conn, session_id)
        monkeypatch.setattr(store, "list_entries", real)
        books.edit(
            UUID(entries[0].entry.id),
            OWNER.name,
            {"steps": SectionEdit(claims=("Restart twice.",))},
        )
        return entries

    monkeypatch.setattr(store, "list_entries", listing)
    assert books.summaries(session) == before
    assert books.summaries(session)[0].stored.version == before[0].stored.version + 1


async def test_follow_up_sources_remain_scoped_to_the_linked_entry_and_topic(
    scratch: Scratch, monkeypatch: pytest.MonkeyPatch
) -> None:
    done = await answered_follow_up(scratch)
    books = RunbookService(scratch.dsn)
    base = books.get(done.world.billing)
    with acting_as(OWNER):
        books.apply_proposal(
            proposals.proposal_id(done.session_id, base.entry.topic_id),
            OWNER.name,
            {"rollback": "merge", "contacts": "merge"},
            expected_revision=1,
            expected_version=base.version,
        )
    expected = books.unsupported(base.session_id)
    loaded: list[UUID] = []
    checked: dict[str, set[str]] = {}
    real_load, real_check = interviews.load_turns, faithfulness.check_entry

    def load(conn: psycopg.Connection, session: UUID) -> Any:
        loaded.append(session)
        return real_load(conn, session)

    def check(entry: Any, *args: Any, **kwargs: Any) -> Any:
        checked[entry.id] = set(kwargs["linked"])
        return real_check(entry, *args, **kwargs)

    monkeypatch.setattr(interviews, "load_turns", load)
    monkeypatch.setattr(faithfulness, "check_entry", check)
    summaries = books.summaries(base.session_id)
    assert {s.stored.entry.id: s.unsupported for s in summaries} == expected
    assert loaded.count(base.session_id) == loaded.count(done.session_id) == 1
    assert checked[base.entry.id] == {str(done.session_id)}
    assert all(not links for key, links in checked.items() if key != base.entry.id)
    assert books.detail(done.world.billing).unsupported == expected[base.entry.id]
    # A proposal for another topic must not grant transcript access to this entry.
    with scratch.connect() as conn:
        conn.execute(
            "UPDATE follow_up_proposals SET topic_id=%s WHERE session_id=%s",
            ("unrelated-topic", done.session_id),
        )
    books.detail(done.world.billing)
    assert checked[base.entry.id] == set()


async def test_reader_content_and_metadata_share_a_snapshot(
    scratch: Scratch, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = await search_world(scratch, indexed=False)
    books = RunbookService(scratch.dsn)
    before = books.reader_view(world.billing, READER)
    real = access.approved_entry

    def approved(conn: psycopg.Connection, *args: Any, **kwargs: Any) -> store.StoredEntry:
        stored = real(conn, *args, **kwargs)
        books.edit(world.billing, OWNER.name, {"steps": SectionEdit(claims=("Restart twice.",))})
        return stored

    monkeypatch.setattr(access, "approved_entry", approved)
    assert books.reader_view(world.billing, READER) == before
    with pytest.raises(NotFoundError):
        books.reader_view(world.billing, READER)


async def test_read_operations_reject_missing_resources_and_allow_empty_sessions(
    scratch: Scratch,
) -> None:
    session = await finished(scratch)
    books = RunbookService(scratch.dsn)
    with pytest.raises(NotFoundError):
        books.detail(uuid4())
    with pytest.raises(NotFoundError):
        books.summaries(uuid4())
    with scratch.connect() as conn:
        conn.execute("DELETE FROM runbook_entries WHERE session_id=%s", (session,))
    assert books.summaries(session) == ()
