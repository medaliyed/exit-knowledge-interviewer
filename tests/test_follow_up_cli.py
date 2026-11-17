"""F22 terminal parity: authenticated, recoverable follow-up work through shared services."""

import asyncio
import json
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr

from eki import cli
from eki.auth import Principal
from eki.config import Settings
from eki.followup.gaps import GapService
from eki.ingest.redaction import RedactedText
from eki.interview import store
from eki.interview.models import InterviewPlan
from eki.interview.plan import edit_follow_up
from eki.interview.service import SessionView
from eki.runbook.service import RunbookService
from tests.conftest import Scratch
from tests.fakes import ScriptedModel, tool_reply
from tests.followup_support import answered_follow_up, gap_world, triaged_world
from tests.interview_support import make_service
from tests.search_support import OWNER, READER

pytestmark = [pytest.mark.db, pytest.mark.anyio]
TOKEN = "follow-up-cli-credential-01234567890123456789"


def identity(monkeypatch: pytest.MonkeyPatch, scratch: Scratch, principal: Principal) -> None:
    settings = Settings(
        _env_file=None,
        database_url=SecretStr(scratch.dsn),
        api_token=SecretStr(TOKEN),
        api_tokens=SecretStr(json.dumps([{"token": TOKEN, **principal.__dict__}])),
    )
    monkeypatch.setattr(cli, "get_settings", lambda: settings)


async def command(capsys: pytest.CaptureFixture[str], *args: str) -> dict[str, Any]:
    assert await asyncio.to_thread(cli.main, ["follow-up", *args]) == 0
    return json.loads(capsys.readouterr().out)  # type: ignore[no-any-return]


async def test_reader_saves_idempotent_safe_request_and_cannot_read_owner_queue(
    scratch: Scratch, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = await gap_world(scratch)
    identity(monkeypatch, scratch, READER)
    rid = str(uuid4())
    args = ["save", "--run", str(world.run_id), "--request-id", rid, "--question", "Who signs?"]
    first = await command(capsys, *args)
    assert first["created"] is True
    again = await command(capsys, *args)
    assert again["created"] is False and again["receipt"] == first["receipt"]
    receipt = await command(capsys, "receipt", "--gap", first["receipt"]["id"])
    assert receipt["question"] == "Who signs?" and "requester_subject" not in receipt
    assert cli.main(["follow-up", "gaps", "--run", str(world.run_id)]) == 1
    assert "run not found" in capsys.readouterr().err
    identity(monkeypatch, scratch, OWNER)
    mapped = await command(
        capsys,
        "triage",
        "--run",
        str(world.run_id),
        "--gap",
        receipt["id"],
        "--expected-revision",
        str(receipt["revision"]),
        "--action",
        "map",
        "--target-entry",
        str(world.billing),
        "--target-section",
        "contacts",
        "--seed-question",
        "Who signs the export?",
        "--priority",
        "7",
    )
    assert mapped["target_entry_id"] == str(world.billing) and mapped["priority"] == 7


async def test_prepare_review_and_discard_need_no_models_and_keep_revisions(
    scratch: Scratch, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world, rollback, request = await triaged_world(scratch)
    with scratch.connect() as conn:
        baseline = conn.execute("SELECT count(*) FROM audit_events WHERE kind='model'").fetchone()
    identity(monkeypatch, scratch, OWNER)
    page = await command(capsys, "gaps", "--run", str(world.run_id))
    assert {g["id"] for g in page["gaps"]} == {rollback, request}
    prepared = await command(
        capsys,
        "plan",
        "--run",
        str(world.run_id),
        "--parent-session",
        str(world.session_id),
        "--gap",
        rollback,
        "--gap",
        request,
        "--max-questions",
        "2",
    )
    assert prepared["kind"] == "follow_up" and prepared["limits"]["questions"] == 2
    edited = await command(
        capsys,
        "edit-plan",
        "--plan",
        prepared["id"],
        "--expected-revision",
        "1",
        "--remove-gap",
        request,
        "--seed",
        f"{rollback}=How do we restore safely?",
    )
    assert edited["revision"] == 2 and len(edited["topics"][0]["questions"]) == 1
    assert edited["topics"][0]["questions"][0]["text"] == "How do we restore safely?"
    shown = await command(capsys, "show-plan", "--plan", prepared["id"])
    assert shown == edited
    assert (
        cli.main(
            [
                "follow-up",
                "discard",
                "--plan",
                prepared["id"],
                "--expected-revision",
                "1",
            ]
        )
        == 1
    )
    capsys.readouterr()
    discarded = await command(
        capsys,
        "discard",
        "--plan",
        prepared["id"],
        "--expected-revision",
        "2",
    )
    assert discarded["topics"] == []
    with scratch.connect() as conn:
        assert (
            conn.execute("SELECT count(*) FROM audit_events WHERE kind='model'").fetchone()
            == baseline
        )


async def test_proposal_comparison_apply_and_explicit_publication_resolution(
    scratch: Scratch, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    done = await answered_follow_up(scratch)
    identity(monkeypatch, scratch, READER)
    assert cli.main(["follow-up", "proposals", "--session", str(done.session_id)]) == 1
    capsys.readouterr()
    identity(monkeypatch, scratch, OWNER)
    found = await command(capsys, "proposals", "--session", str(done.session_id))
    (proposal,) = found["proposals"]
    assert proposal["status"] == "pending" and proposal["sections"]
    applied = await command(
        capsys,
        "apply",
        "--proposal",
        proposal["id"],
        "--expected-revision",
        "1",
        "--expected-version",
        str(proposal["entry_version"]),
        "--section",
        "rollback=merge",
        "--section",
        "contacts=merge",
    )
    assert applied["entry"]["status"] == "draft"
    assert applied["entry"]["id"] == str(done.world.billing)
    books = RunbookService(scratch.dsn)
    from eki.auth import acting_as

    with acting_as(OWNER):
        approved = books.approve(done.world.billing, OWNER.name)
    gaps = GapService(scratch.dsn).queue(done.world.run_id, OWNER, filter="all")["gaps"]
    request = next(g for g in gaps if g["id"] == done.request_gap)
    resolved = await command(
        capsys,
        "resolve",
        "--run",
        str(done.world.run_id),
        "--gap",
        done.request_gap,
        "--entry",
        str(done.world.billing),
        "--expected-revision",
        str(request["revision"]),
        "--expected-version",
        str(approved.version),
        "--confirms-answer",
    )
    assert resolved["status"] == "resolved" and resolved["resolved_version"] == approved.version


async def test_follow_up_interview_binds_real_identity_and_stop_preserves_work(
    scratch: Scratch, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world, rollback, _ = await triaged_world(scratch)
    identity(monkeypatch, scratch, OWNER)
    prepared = await command(
        capsys,
        "plan",
        "--run",
        str(world.run_id),
        "--parent-session",
        str(world.session_id),
        "--gap",
        rollback,
    )
    service = make_service(
        scratch.dsn,
        agent=None,
        interviewer=ScriptedModel(
            [
                tool_reply(
                    (
                        "ask_expert",
                        {
                            "question": prepared["topics"][0]["questions"][0]["text"],
                            "section": "rollback",
                            "gap_id": rollback,
                        },
                    )
                )
            ]
        ),
    )
    monkeypatch.setattr(cli.interview, "build_service", lambda settings: service)
    identity(monkeypatch, scratch, Principal("impostor", OWNER.name))
    assert await asyncio.to_thread(cli.main, ["interview", "--plan", prepared["id"]]) == 1
    capsys.readouterr()
    with scratch.connect() as conn:
        assert store.session_of_plan(conn, UUID(prepared["id"])) is None
    identity(monkeypatch, scratch, OWNER)
    assert (
        await asyncio.to_thread(
            cli.main,
            ["interview", "--plan", prepared["id"], "--approve-as", "Somebody else"],
        )
        == 1
    )
    capsys.readouterr()
    monkeypatch.setattr("builtins.input", lambda prompt="": "/stop")
    assert await asyncio.to_thread(cli.main, ["interview", "--plan", prepared["id"]]) == 0
    output = capsys.readouterr().out
    assert "follow-up" in output.lower() and "expert_stopped" in output
    with scratch.connect() as conn:
        sid = store.session_of_plan(conn, UUID(prepared["id"]))
        assert sid is not None and store.load_session(conn, sid).status == "finished"  # type: ignore[union-attr]


async def test_stale_proposal_requires_explicit_refresh_and_identical_apply_replays(
    scratch: Scratch, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    done = await answered_follow_up(scratch)
    identity(monkeypatch, scratch, OWNER)
    listed = await command(capsys, "proposals", "--session", str(done.session_id))
    (proposal,) = listed["proposals"]
    from eki.runbook.review import SectionEdit

    books = RunbookService(scratch.dsn)
    current = books.edit(
        done.world.billing, OWNER.name, {"rationale": SectionEdit(claims=("Retain the export.",))}
    )
    old = [
        "apply",
        "--proposal",
        proposal["id"],
        "--expected-revision",
        "1",
        "--expected-version",
        str(proposal["entry_version"]),
        "--section",
        "rollback=merge",
    ]
    assert cli.main(["follow-up", *old]) == 1
    assert "changed" in capsys.readouterr().err
    refreshed = await command(
        capsys,
        "refresh",
        "--proposal",
        proposal["id"],
        "--expected-revision",
        "1",
        "--expected-version",
        str(current.version),
    )
    assert refreshed["revision"] == 2 and refreshed["stale"] is False
    apply = [
        "apply",
        "--proposal",
        proposal["id"],
        "--expected-revision",
        "2",
        "--expected-version",
        str(current.version),
        "--section",
        "rollback=merge",
    ]
    first = await command(capsys, *apply)
    replay = await command(capsys, *apply)
    assert first["replayed"] is False and replay["replayed"] is True
    assert replay["entry"]["version"] == first["entry"]["version"]


async def test_cli_does_not_approve_a_concurrently_changed_follow_up_plan(
    scratch: Scratch, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world, rollback, _ = await triaged_world(scratch)
    identity(monkeypatch, scratch, OWNER)
    prepared = await command(
        capsys,
        "plan",
        "--run",
        str(world.run_id),
        "--parent-session",
        str(world.session_id),
        "--gap",
        rollback,
    )
    service = make_service(scratch.dsn, agent=None)
    approve = service.approve

    async def concurrent_edit(
        plan_id: UUID, by: str, *, expected_revision: int | None = None
    ) -> SessionView:
        assert expected_revision == 1, "CLI approval must bind the plan shown to the expert"

        def changed(plan: InterviewPlan) -> InterviewPlan:
            return edit_follow_up(
                plan,
                seeds={rollback: RedactedText("Which backup should we restore?")},
                remove_gaps=[],
            )

        await service.edit_plan(plan_id, changed, expected_revision=1)
        return await approve(plan_id, by, expected_revision=expected_revision)

    monkeypatch.setattr(service, "approve", concurrent_edit)
    monkeypatch.setattr(cli.interview, "build_service", lambda settings: service)
    assert await asyncio.to_thread(cli.main, ["interview", "--plan", prepared["id"]]) == 1
    assert "plan changed" in capsys.readouterr().err
    with scratch.connect() as conn:
        assert store.session_of_plan(conn, UUID(prepared["id"])) is None
