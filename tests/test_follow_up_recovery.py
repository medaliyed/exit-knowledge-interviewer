"""F22 scripted regressions for seed isolation, provider budgets and restart boundaries."""

import asyncio
from collections.abc import Sequence
from typing import Any
from uuid import UUID, uuid4

import pytest

from eki.audit import AuditError, append_event
from eki.auth import acting_as
from eki.errors import ConflictError
from eki.followup import sessions as follow_ups
from eki.followup.gaps import GapService
from eki.followup.sessions import FollowUpService
from eki.ingest.embedding import HashingEmbedder
from eki.ingest.redaction import RedactedText
from eki.interview import store
from eki.interview.models import FollowUpLimits
from eki.interview.plan import edit_follow_up
from eki.llm import ModelError
from eki.runbook.models import InterviewTurnCitation
from eki.runbook.review import SectionEdit
from eki.runbook.service import RunbookService
from tests.conftest import Scratch
from tests.fakes import ScriptedModel, tool_reply
from tests.followup_support import ask, note, prepare, triaged_world
from tests.interview_support import make_service, say
from tests.search_support import OWNER, READER

pytestmark = [pytest.mark.db, pytest.mark.anyio]


async def test_two_selected_questions_in_one_section_keep_both_facts(scratch: Scratch) -> None:
    world, first_gap, _ = await triaged_world(scratch)
    gaps = GapService(scratch.dsn)
    second, _ = gaps.save_request(
        world.run_id, READER, request_id=uuid4(), question="Who authorises a rollback?"
    )
    gaps.triage(
        world.run_id,
        second["id"],
        OWNER,
        action="map",
        expected_revision=second["revision"],
        target_entry_id=world.billing,
        target_section="rollback",
    )
    plan = prepare(scratch, world, [first_gap, second["id"]])
    one, two = (q.text for q in plan.topics[0].questions)
    model = ScriptedModel(
        [
            tool_reply(ask(one, "rollback", first_gap)),
            tool_reply(note("rollback", "Restore the export."), ("finish_topic", {})),
            tool_reply(ask(two, "rollback", second["id"])),
            tool_reply(note("rollback", "Finance authorises it."), ("finish_topic", {})),
        ]
    )
    books = RunbookService(scratch.dsn)
    previous = books.get(world.billing)
    service = make_service(scratch.dsn, agent=None, interviewer=model)
    with acting_as(OWNER):
        view = await service.approve(plan.id, OWNER.name)
        events = await say(service, view.session.id, "Restore the export.")
        assert events[-1]["question"] == two
        assert "not been attempted" in model.sent_text()
        assert (await say(service, view.session.id, "Finance authorises it."))[-1]["type"] == "done"
    final = await service.view(view.session.id)
    assert [n.text for n in final.notes] == ["Restore the export.", "Finance authorises it."]
    assert len({n.turn for n in final.notes}) == 2
    assert books.get(world.billing) == previous
    assert books.synthesise(view.session.id) == (), "mapped targets never become duplicate entries"
    outcomes = {
        g["id"]: g["attempts"][-1]["outcome"]
        for g in gaps.queue(world.run_id, OWNER)["gaps"]
        if g["attempts"]
    }
    assert outcomes[first_gap] == outcomes[second["id"]] == "answered"


@pytest.mark.parametrize("response", ["skip", "unknown", "confidential"])
async def test_unanswered_outcomes_remain_private_and_unresolved(
    scratch: Scratch, response: str
) -> None:
    world, gap, _ = await triaged_world(scratch)
    plan = prepare(scratch, world, [gap])
    seed = plan.topics[0].questions[0].text
    later = (
        ("mark_open", {"section": "rollback", "reason": "I do not know"})
        if response == "unknown"
        else note("rollback", "A fabricated public fact.")
    )
    model = ScriptedModel(
        [
            tool_reply(ask(seed, "rollback", gap)),
            tool_reply(later, ("finish_topic", {})),
        ]
    )
    service = make_service(scratch.dsn, agent=None, interviewer=model)
    with acting_as(OWNER):
        view = await service.approve(plan.id, OWNER.name)
        await say(
            service,
            view.session.id,
            "PRIVATE_FOLLOW_UP_SENTINEL" if response == "confidential" else "I do not know",
            action="skip" if response == "skip" else "answer",
            confidential=response == "confidential",
        )
    final = await service.view(view.session.id)
    assert final.session.status == "finished"
    assert not [n for n in final.notes if not n.confidential]
    assert "PRIVATE_FOLLOW_UP_SENTINEL" not in model.sent_text()
    (selected,) = [
        g for g in GapService(scratch.dsn).queue(world.run_id, OWNER)["gaps"] if g["id"] == gap
    ]
    assert selected["status"] == "pending"
    assert selected["attempts"][-1]["outcome"] == ("skipped" if response == "skip" else response)


async def test_cached_final_reply_replays_after_budget_is_spent(
    scratch: Scratch,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world, gap, _ = await triaged_world(scratch)
    plan = prepare(scratch, world, [gap], limits=FollowUpLimits(questions=1, model_calls=2))
    seed = plan.topics[0].questions[0].text
    model = ScriptedModel(
        [
            tool_reply(ask(seed, "rollback", gap)),
            tool_reply(note("rollback", "Restore the export."), ("finish_topic", {})),
        ]
    )
    service = make_service(scratch.dsn, agent=None, interviewer=model)
    original = store.record_note
    failed = False

    def crash_after_note(*args: Any, **kwargs: Any) -> None:
        nonlocal failed
        original(*args, **kwargs)
        if not failed:
            failed = True
            raise ModelError("simulated crash after a durable tool effect")

    monkeypatch.setattr(store, "record_note", crash_after_note)
    with acting_as(OWNER):
        view = await service.approve(plan.id, OWNER.name)
        events = await say(service, view.session.id, "Restore the export.", message_id=uuid4())
        assert events[-1]["code"] == "model_unavailable"
        mid = UUID(events[-1]["message_id"])
        restarted = make_service(scratch.dsn, agent=None, interviewer=model)
        recovered = [e async for e in await restarted.resume(view.session.id, mid)]
        assert recovered[-1] == {"type": "done"}
        assert (await restarted.receipt(view.session.id, mid))["status"] == "processed"
    assert len(model.calls) == 2
    final = await restarted.view(view.session.id)
    assert final.session.status == "finished" and len(final.notes) == 1
    assert final.usage is not None and final.usage["model_calls"] == 2
    assert final.usage["input_tokens"] == 0 and final.usage["duration_seconds"] >= 0
    with scratch.connect() as conn:
        assert conn.execute(
            "SELECT count(*) FROM session_provider_attempts p JOIN audit_events a ON "
            "a.call_id=p.call_id AND a.kind='model' AND a.phase='started' WHERE p.session_id=%s",
            (view.session.id,),
        ).fetchone() == (2,)


async def test_exhausted_initial_call_recovers_to_a_finished_session(scratch: Scratch) -> None:
    world, gap, _ = await triaged_world(scratch)
    plan = prepare(scratch, world, [gap], limits=FollowUpLimits(model_calls=1))
    model = ScriptedModel([ModelError("provider crashed")])
    service = make_service(scratch.dsn, agent=None, interviewer=model)
    with acting_as(OWNER):
        with pytest.raises(ModelError):
            await service.approve(plan.id, OWNER.name)
        recovered = await service.approve(plan.id, OWNER.name)
    assert recovered.session.status == "finished"
    assert recovered.session.completion_reason == "model_budget"
    assert len(model.calls) == 1
    assert recovered.turns == () and recovered.notes == ()


async def test_audit_failure_rolls_back_provider_reservation(
    scratch: Scratch,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world, gap, _ = await triaged_world(scratch)
    plan = prepare(scratch, world, [gap])
    seed = plan.topics[0].questions[0].text
    model = ScriptedModel([tool_reply(ask(seed, "rollback", gap))])
    service = make_service(scratch.dsn, agent=None, interviewer=model)
    original = append_event

    def refuse(*args: Any, **kwargs: Any) -> None:
        if len(args) > 2 and args[1:3] == ("model", "started"):
            raise AuditError("audit offline")
        original(*args, **kwargs)

    with acting_as(OWNER):
        with monkeypatch.context() as patch:
            patch.setattr(follow_ups, "append_event", refuse)
            with pytest.raises(AuditError):
                await service.approve(plan.id, OWNER.name)
        assert not model.calls
        with scratch.connect() as conn:
            assert conn.execute("SELECT count(*) FROM session_provider_attempts").fetchone() == (0,)
        view = await service.approve(plan.id, OWNER.name)
    assert view.usage is not None and view.usage["model_calls"] == 1


class SemanticDouble:
    name = "litellm:scripted-embedding:256"
    dimensions = 256

    def __init__(self) -> None:
        self.calls = 0

    def embed(self, texts: Sequence[RedactedText]) -> list[list[float]]:
        self.calls += 1
        return HashingEmbedder().embed(texts)


async def test_semantic_embedding_budget_survives_request_restarts(scratch: Scratch) -> None:
    world, gap, _ = await triaged_world(scratch)
    plan = prepare(scratch, world, [gap], limits=FollowUpLimits(embedding_calls=1))
    seed = plan.topics[0].questions[0].text
    embedder = SemanticDouble()
    with scratch.connect() as conn:
        conn.execute(
            "UPDATE ingest_runs SET artifact_embedding_model=%s WHERE id=%s",
            (embedder.name, world.run_id),
        )
    search = ("search_artifacts", {"query": "rollback"})
    model = ScriptedModel(
        [
            tool_reply(search, ask(seed, "rollback", gap)),
            tool_reply(search, note("rollback", "Restore it."), ("finish_topic", {})),
        ]
    )
    service = make_service(scratch.dsn, agent=None, interviewer=model)
    service.embedder = embedder
    with acting_as(OWNER):
        view = await service.approve(plan.id, OWNER.name)
        restarted = make_service(scratch.dsn, agent=None, interviewer=model)
        restarted.embedder = embedder
        await say(restarted, view.session.id, "Restore it.")
    assert embedder.calls == 1
    assert FollowUpService(scratch.dsn).usage(view.session.id)["embedding_calls"] == 1
    with scratch.connect() as conn:
        assert conn.execute(
            "SELECT count(*) FROM audit_events WHERE kind='tool' AND phase='refused' "
            "AND details->>'operation'='search_artifacts'",
        ).fetchone() == (1,)


async def test_edit_releases_removed_seeds_and_freezes_reviewed_limits(scratch: Scratch) -> None:
    world, one, two = await triaged_world(scratch)
    rid = uuid4()
    plan = prepare(scratch, world, [one, two], request_id=rid)
    service = make_service(scratch.dsn, agent=None)
    revised = await service.edit_plan(
        plan.id,
        lambda p: edit_follow_up(
            p,
            seeds={one: RedactedText("Where is the key? token=sk-testA1BCDEFGHIJKLMNOPQRSTUVWX")},
            remove_gaps=[two],
            limits=FollowUpLimits(questions=1),
        ),
        expected_revision=plan.revision,
    )
    assert revised.revision == plan.revision + 1
    assert revised.topics[0].sections == ("rollback",)
    assert "sk-test" not in revised.topics[0].questions[0].text
    assert prepare(scratch, world, [one, two], request_id=rid) == revised
    with pytest.raises(ConflictError, match="plan changed"):
        await service.edit_plan(plan.id, lambda p: p, expected_revision=plan.revision)
    with acting_as(OWNER), pytest.raises(ConflictError, match="plan changed"):
        await service.approve(plan.id, OWNER.name, expected_revision=plan.revision)
    pending = {
        g["id"]: g["status"] for g in GapService(scratch.dsn).queue(world.run_id, OWNER)["gaps"]
    }
    assert pending[two] == "pending" and pending[one] == "scheduled"


async def test_parallel_prepare_reserves_only_one_plan(scratch: Scratch) -> None:
    world, gap, _ = await triaged_world(scratch)
    results = await asyncio.gather(
        *[asyncio.to_thread(prepare, scratch, world, [gap]) for _ in range(2)],
        return_exceptions=True,
    )
    assert len([r for r in results if isinstance(r, ConflictError)]) == 1
    with scratch.connect() as conn:
        assert conn.execute(
            "SELECT count(*) FROM interview_plans WHERE kind='follow_up'"
        ).fetchone() == (1,)


async def test_changed_target_cannot_start_a_prepared_follow_up(scratch: Scratch) -> None:
    world, gap, _ = await triaged_world(scratch)
    plan = prepare(scratch, world, [gap])
    RunbookService(scratch.dsn).edit(
        world.billing,
        OWNER.name,
        {"trigger": SectionEdit(claims=("Updated trigger.",))},
    )
    model = ScriptedModel([])
    service = make_service(scratch.dsn, agent=None, interviewer=model)
    with acting_as(OWNER), pytest.raises(ConflictError, match="target changed"):
        await service.approve(plan.id, OWNER.name)
    assert not model.calls


async def test_new_topic_finishes_partial_with_a_cited_private_draft(scratch: Scratch) -> None:
    world, _, _ = await triaged_world(scratch)
    gaps = GapService(scratch.dsn)
    gap, _ = gaps.save_request(
        world.run_id, READER, request_id=uuid4(), question="When do vendor renewals start?"
    )
    gaps.triage(
        world.run_id,
        gap["id"],
        OWNER,
        action="new_topic",
        expected_revision=gap["revision"],
        topic_label="Vendor renewals",
    )
    plan = prepare(scratch, world, [gap["id"]], limits=FollowUpLimits(questions=1))
    model = ScriptedModel(
        [
            tool_reply(ask(plan.topics[0].questions[0].text, "trigger", gap["id"])),
            tool_reply(
                note("trigger", "Begin thirty days before expiry."),
                ask("Who approves?", "contacts", gap["id"]),
            ),
        ]
    )
    service = make_service(scratch.dsn, agent=None, interviewer=model)
    with acting_as(OWNER):
        view = await service.approve(plan.id, OWNER.name)
        assert len(view.checklist) == 7
        await say(service, view.session.id, "Begin thirty days before expiry.")
    final = await service.view(view.session.id)
    assert final.usage is not None and final.usage["partial"]
    assert final.session.completion_reason == "question_limit"
    books = RunbookService(scratch.dsn)
    (draft,) = books.list(view.session.id)
    assert draft.entry.status == "draft"
    assert not draft.entry.missing_sections()
    citation = draft.entry.trigger.claims[0].citations[0]
    assert isinstance(citation, InterviewTurnCitation)
    assert citation.session_id == str(view.session.id)
    assert len(final.open_items) == 6
    assert all(draft.entry.section(item.section).open_question for item in final.open_items)
    assert books.synthesise(view.session.id) == (draft,)


async def test_unknown_second_seed_does_not_erase_the_first_fact(scratch: Scratch) -> None:
    world, one, two = await triaged_world(scratch)
    gaps = GapService(scratch.dsn)
    request = gaps.receipt(two, OWNER)
    gaps.triage(
        world.run_id,
        two,
        OWNER,
        action="map",
        expected_revision=request["revision"],
        target_entry_id=world.billing,
        target_section="rollback",
    )
    plan = prepare(scratch, world, [one, two])
    seed1, seed2 = (q.text for q in plan.topics[0].questions)
    model = ScriptedModel(
        [
            tool_reply(ask(seed1, "rollback", one)),
            tool_reply(note("rollback", "Restore the export."), ask(seed2, "rollback", two)),
            tool_reply(
                ("mark_open", {"section": "rollback", "reason": "I do not know", "gap_id": two}),
                note("rollback", "Invented."),
                ("finish_topic", {}),
            ),
        ]
    )
    service = make_service(scratch.dsn, agent=None, interviewer=model)
    with acting_as(OWNER):
        view = await service.approve(plan.id, OWNER.name)
        await say(service, view.session.id, "Restore the export.")
        await say(service, view.session.id, "I do not know.")
    final = await service.view(view.session.id)
    assert [n.text for n in final.notes] == ["Restore the export."]
    selected = {g["id"]: g for g in gaps.queue(world.run_id, OWNER)["gaps"]}
    assert selected[one]["attempts"][-1]["outcome"] == "answered"
    assert (
        selected[two]["status"] == "pending"
        and selected[two]["attempts"][-1]["outcome"] == "unknown"
    )
    assert final.usage is not None and final.usage["partial"]


async def test_token_usage_reads_only_the_sessions_own_run(scratch: Scratch) -> None:
    from eki.audit import AuditDetails
    from tests.followup_support import answered_follow_up

    done = await answered_follow_up(scratch)
    service = FollowUpService(scratch.dsn)
    before = service.usage(done.session_id)
    with scratch.connect() as conn:
        # Follow-up model events are always written under the session's run, so the
        # sum can use the run index instead of scanning the whole, ever-growing table.
        append_event(
            conn,
            "model",
            "completed",
            AuditDetails(
                operation="interview",
                target_id=done.session_id,
                input_tokens=1000,
                output_tokens=1000,
            ),
            run_id=uuid4(),
        )
        append_event(
            conn,
            "model",
            "completed",
            AuditDetails(
                operation="interview", target_id=done.session_id, input_tokens=7, output_tokens=3
            ),
            run_id=done.world.run_id,
        )
    after = service.usage(done.session_id)
    assert after["input_tokens"] == (before["input_tokens"] or 0) + 7
    assert after["output_tokens"] == (before["output_tokens"] or 0) + 3
