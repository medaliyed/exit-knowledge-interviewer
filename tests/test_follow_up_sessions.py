"""F22 9b: deterministic follow-up plans, respondent authority, scope and durable budgets."""

import json
from typing import Any
from uuid import UUID, uuid4

import pytest

from eki import maintenance
from eki.auth import AuthenticationError, Principal, acting_as
from eki.errors import ConflictError, NotFoundError
from eki.followup.gaps import GapService, open_section_gap_id
from eki.followup.sessions import DEFAULT_LIMITS, FollowUpService
from eki.interview.models import FollowUpLimits
from eki.interview.plan import remove_topic
from eki.llm import ModelError, Reply
from eki.runbook.service import RunbookService
from tests.conftest import Scratch
from tests.fakes import ScriptedModel, tool_reply
from tests.followup_support import GapWorld, gap_world
from tests.interview_support import TOPIC_IDS, make_service, say
from tests.search_support import ADMIN, OWNER, READER

pytestmark = [pytest.mark.db, pytest.mark.anyio]
IMPOSTOR = Principal("impostor", OWNER.name)


def ask(question: str, section: str, gap_id: str | None = None) -> tuple[str, dict[str, Any]]:
    arguments: dict[str, Any] = {"question": question, "section": section}
    if gap_id is not None:
        arguments["gap_id"] = gap_id
    return ("ask_expert", arguments)


def note(section: str, text: str) -> tuple[str, dict[str, Any]]:
    return ("record_note", {"section": section, "text": text})


async def triaged_world(scratch: Scratch) -> tuple[GapWorld, str, str]:
    """The gap world plus one reader question mapped to billing/contacts. Returns both gap IDs."""
    world = await gap_world(scratch)
    service = GapService(scratch.dsn)
    saved, _ = service.save_request(
        world.run_id,
        READER,
        request_id=uuid4(),
        question="Who authorises reopening a closed period?",
    )
    service.triage(
        world.run_id,
        saved["id"],
        OWNER,
        action="map",
        expected_revision=saved["revision"],
        target_entry_id=world.billing,
        target_section="contacts",
    )
    return world, str(open_section_gap_id(world.billing, "rollback")), saved["id"]


def prepare(
    scratch: Scratch,
    world: GapWorld,
    gap_ids: list[str],
    *,
    who: Principal = OWNER,
    request_id: UUID | None = None,
    limits: FollowUpLimits | None = None,
) -> Any:
    return FollowUpService(scratch.dsn).prepare(
        world.run_id,
        who,
        request_id=request_id or uuid4(),
        parent_session_id=world.session_id,
        gap_ids=gap_ids,
        limits=limits,
    )


def tool_results(model: ScriptedModel, call_index: int) -> list[str]:
    return [m.content for m in model.calls[call_index].messages if m.role == "tool"]


# --- I1 / I3 / A4: deterministic preparation without a model ---


async def test_prepare_builds_a_scoped_plan_without_model_calls(scratch: Scratch) -> None:
    world, rollback_gap, request_gap = await triaged_world(scratch)
    request_id = uuid4()
    plan = prepare(scratch, world, [rollback_gap, request_gap], request_id=request_id)
    assert plan.id == request_id and plan.kind == "follow_up" and plan.status == "draft"
    assert plan.parent_session_id == world.session_id and plan.run_id == world.run_id
    assert plan.limits == DEFAULT_LIMITS
    (topic,) = plan.topics
    assert topic.topic_id == TOPIC_IDS[0] and topic.origin == "follow_up"
    assert topic.sections == ("rollback", "contacts")
    assert topic.target_entry_id == str(world.billing)
    assert topic.base_version == RunbookService(scratch.dsn).get(world.billing).version
    assert [(q.origin, q.gap_id) for q in topic.questions] == [
        ("follow_up", rollback_gap),
        ("follow_up", request_gap),
    ]
    assert "Who authorises reopening a closed period?" in topic.questions[1].text
    assert all(q.artifact_id is None and q.quote is None for q in topic.questions)
    again = prepare(scratch, world, [rollback_gap, request_gap], request_id=request_id)
    assert again == plan, "the same request returns the same plan"
    statuses = {
        g["id"]: g["status"] for g in GapService(scratch.dsn).queue(world.run_id, OWNER)["gaps"]
    }
    assert statuses == {rollback_gap: "scheduled", request_gap: "scheduled"}
    with scratch.connect() as conn:
        assert conn.execute(
            "SELECT count(*) FROM plan_generations WHERE id=%s", (plan.id,)
        ).fetchone() == (0,)
        assert conn.execute(
            "SELECT count(*) FROM audit_events WHERE kind='model' AND details->>'target_id'=%s",
            (str(plan.id),),
        ).fetchone() == (0,)


async def test_prepare_refuses_unready_selections(scratch: Scratch) -> None:
    world, rollback_gap, request_gap = await triaged_world(scratch)
    service = GapService(scratch.dsn)
    unmapped, _ = service.save_request(
        world.run_id, READER, request_id=uuid4(), question="Where are the vendor contracts?"
    )
    with pytest.raises(ValueError, match="triage"):
        prepare(scratch, world, [unmapped["id"]])
    with pytest.raises(ValueError, match="question limit"):
        prepare(
            scratch,
            world,
            [rollback_gap, request_gap],
            limits=FollowUpLimits(questions=1),
        )
    with pytest.raises(NotFoundError):
        prepare(scratch, world, [str(uuid4())])
    for who in (READER, IMPOSTOR):
        with pytest.raises(NotFoundError):
            prepare(scratch, world, [rollback_gap], who=who)
    first = prepare(scratch, world, [rollback_gap])
    with pytest.raises(ConflictError, match="active follow-up"):
        prepare(scratch, world, [request_gap])
    with pytest.raises(ConflictError, match="scheduled"):
        service.triage(
            world.run_id,
            rollback_gap,
            OWNER,
            action="defer",
            expected_revision=1,
            reason="Already in a plan",
        )
    interviews = make_service(scratch.dsn, agent=None)
    await interviews.edit_plan(first.id, lambda p: remove_topic(p, TOPIC_IDS[0]))
    (gap,) = [g for g in service.queue(world.run_id, OWNER)["gaps"] if g["id"] == rollback_gap]
    assert gap["status"] == "pending", "removing the topic releases its reservation"
    with scratch.connect() as conn:
        conn.execute("UPDATE sessions SET status='active' WHERE id=%s", (world.session_id,))
    with pytest.raises(ConflictError, match="finished"):
        prepare(scratch, world, [rollback_gap], request_id=uuid4())


async def test_maintenance_mode_refuses_follow_up_capture(scratch: Scratch) -> None:
    world, rollback_gap, _ = await triaged_world(scratch)
    successor = Principal("samir", "Samir")
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
    with pytest.raises(ConflictError, match="closed"):
        prepare(scratch, world, [rollback_gap], who=successor)


# --- I2 / A4: only the original expert, as current owner, starts and answers ---


async def test_only_the_original_expert_approves_and_answers(scratch: Scratch) -> None:
    world, rollback_gap, _ = await triaged_world(scratch)
    plan = prepare(scratch, world, [rollback_gap])
    seed = plan.topics[0].questions[0].text
    model = ScriptedModel([tool_reply(ask(seed, "rollback", rollback_gap))] * 3, name="interviewer")
    interviews = make_service(scratch.dsn, agent=None, interviewer=model)
    with pytest.raises(AuthenticationError):
        await interviews.approve(plan.id, OWNER.name)
    for who in (READER, ADMIN, IMPOSTOR):
        with acting_as(who), pytest.raises(NotFoundError):
            await interviews.approve(plan.id, who.name)
    with acting_as(OWNER), pytest.raises(ValueError):
        await interviews.approve(plan.id, "Someone Else")
    with acting_as(OWNER):
        view = await interviews.approve(plan.id, OWNER.name)
    assert view.session.status == "active" and view.waiting is not None
    with scratch.connect() as conn:
        assert conn.execute(
            "SELECT respondent_subject FROM sessions WHERE id=%s", (view.session.id,)
        ).fetchone() == (OWNER.subject,)
    for who in (READER, ADMIN, IMPOSTOR):
        with acting_as(who), pytest.raises(NotFoundError):
            await say(interviews, view.session.id, "Not me")
    with pytest.raises(AuthenticationError):
        await say(interviews, view.session.id, "Anonymous")
    with acting_as(OWNER):
        again = await interviews.approve(plan.id, OWNER.name)
    assert again.session.id == view.session.id, "approval retries return the same session"


# --- I5 / I6 / I10 / A5: scoped interview with attributed seeds ---


async def test_scripted_follow_up_stays_in_scope_and_attributes_seeds(scratch: Scratch) -> None:
    world, rollback_gap, request_gap = await triaged_world(scratch)
    plan = prepare(scratch, world, [rollback_gap, request_gap])
    seed1, seed2 = (q.text for q in plan.topics[0].questions)
    model = ScriptedModel(
        [
            tool_reply(ask("Reworded first question?", "rollback", rollback_gap)),
            tool_reply(ask(seed1, "rollback", rollback_gap)),
            tool_reply(
                note("steps", "Out of scope note."),
                ask("Forged?", "contacts", str(uuid4())),
                ask(seed2, "steps", request_gap),
                ask("Unattributed?", "contacts"),
            ),
            tool_reply(
                note("rollback", "Restore the pre-run export."),
                ask(seed2, "contacts", request_gap),
            ),
            tool_reply(
                note("contacts", "Finance operations lead authorises it."),
                ask("Anyone else?", "contacts", request_gap),
            ),
            tool_reply(
                note("contacts", "The CFO signs for amounts over the threshold."),
                ("finish_topic", {}),
            ),
        ],
        name="interviewer",
    )
    interviews = make_service(scratch.dsn, agent=None, interviewer=model)
    with acting_as(OWNER):
        view = await interviews.approve(plan.id, OWNER.name)
        assert view.waiting is not None and view.waiting.text == seed1
        brief = model.calls[0].messages[1].content
        assert "rollback, contacts" in brief and "steps" not in brief.split("Sections")[1]
        assert "must use the approved text" in tool_results(model, 1)[0]
        assert view.checklist == {"rollback": "missing", "contacts": "missing"}
        first = await say(interviews, view.session.id, "Restore the pre-run export.")
        refusals = tool_results(model, 3)
        assert any("outside this follow-up" in r for r in refusals)
        assert any("not a selected gap" in r for r in refusals)
        assert any("gap_id" in r and "Unattributed" not in r for r in refusals)
        assert first[-1]["type"] == "question" and first[-1]["question"] == seed2
        second = await say(interviews, view.session.id, "Finance operations lead.")
        assert second[-1]["type"] == "question" and second[-1]["question"] == "Anyone else?"
        last = await say(interviews, view.session.id, "The CFO above the threshold.")
    assert last[-1]["type"] == "done"
    final = await interviews.view(view.session.id)
    assert final.session.status == "finished"
    assert [(t.text, t.gap_id) for t in final.turns if t.kind == "question"] == [
        (seed1, rollback_gap),
        (seed2, request_gap),
        ("Anyone else?", request_gap),
    ]
    assert sorted(n.section for n in final.notes) == ["contacts", "contacts", "rollback"]
    assert len({n.turn for n in final.notes if n.section == "contacts"}) == 2, (
        "two public facts in one section keep their own source turns"
    )
    assert final.open_items == ()
    usage = FollowUpService(scratch.dsn).usage(view.session.id)
    assert usage["questions"] == 3 and usage["model_calls"] == len(model.calls) == 6
    assert usage["completion_reason"] == "complete"
    statuses = {
        g["id"]: (g["status"], [a["outcome"] for a in g["attempts"]])
        for g in GapService(scratch.dsn).queue(world.run_id, OWNER, filter="all")["gaps"]
    }
    assert statuses[rollback_gap] == ("awaiting_review", ["answered"])
    assert statuses[request_gap] == ("awaiting_review", ["answered"])


async def test_new_topic_uses_the_full_template(scratch: Scratch) -> None:
    world = await gap_world(scratch)
    service = GapService(scratch.dsn)
    saved, _ = service.save_request(
        world.run_id, READER, request_id=uuid4(), question="How are vendor renewals approved?"
    )
    service.triage(
        world.run_id,
        saved["id"],
        OWNER,
        action="new_topic",
        expected_revision=saved["revision"],
        topic_label="Vendor renewal approvals",
    )
    plan = prepare(scratch, world, [saved["id"]])
    (topic,) = plan.topics
    assert topic.label == "Vendor renewal approvals" and topic.target_entry_id is None
    assert topic.sections is None, "a new topic keeps the ordinary seven-section scope"
    assert topic.questions[0].text == "How are vendor renewals approved?"


# --- I7 / I8 / I9 / A6: durable session-wide budgets ---


async def test_question_limit_stops_before_the_next_seed(scratch: Scratch) -> None:
    world, rollback_gap, request_gap = await triaged_world(scratch)
    plan = prepare(
        scratch,
        world,
        [rollback_gap, request_gap],
        limits=FollowUpLimits(questions=2),
    )
    seed1, seed2 = (q.text for q in plan.topics[0].questions)
    model = ScriptedModel(
        [
            tool_reply(ask(seed1, "rollback", rollback_gap)),
            tool_reply(note("rollback", "Restore it."), ask("Clarify?", "rollback", rollback_gap)),
            tool_reply(note("rollback", "Twice."), ask(seed2, "contacts", request_gap)),
        ],
        name="interviewer",
    )
    interviews = make_service(scratch.dsn, agent=None, interviewer=model)
    with acting_as(OWNER):
        view = await interviews.approve(plan.id, OWNER.name)
        await say(interviews, view.session.id, "Restore the export.")
        events = await say(interviews, view.session.id, "Run it twice.")
    assert events[-1] == {"type": "done"}
    final = await interviews.view(view.session.id)
    assert final.session.status == "finished"
    assert [t.text for t in final.turns if t.kind == "question"] == [seed1, "Clarify?"]
    assert [(o.section, o.reason) for o in final.open_items] == [
        ("contacts", "the question limit for this follow-up was reached")
    ]
    usage = FollowUpService(scratch.dsn).usage(view.session.id)
    assert usage["questions"] == 2 and usage["completion_reason"] == "question_limit"
    gaps = {
        g["id"]: g for g in GapService(scratch.dsn).queue(world.run_id, OWNER, filter="all")["gaps"]
    }
    assert gaps[request_gap]["status"] == "pending"
    assert [a["outcome"] for a in gaps[request_gap]["attempts"]] == ["unattempted"]
    assert gaps[rollback_gap]["status"] == "awaiting_review"


async def test_model_budget_closes_the_session_deterministically(scratch: Scratch) -> None:
    world, rollback_gap, request_gap = await triaged_world(scratch)
    plan = prepare(
        scratch,
        world,
        [rollback_gap, request_gap],
        limits=FollowUpLimits(model_calls=2),
    )
    seed1 = plan.topics[0].questions[0].text
    model = ScriptedModel(
        [
            tool_reply(ask(seed1, "rollback", rollback_gap)),
            Reply(text="Thinking out loud instead of using a tool."),
            tool_reply(note("rollback", "Never reached.")),
        ],
        name="interviewer",
    )
    interviews = make_service(scratch.dsn, agent=None, interviewer=model)
    with acting_as(OWNER):
        view = await interviews.approve(plan.id, OWNER.name)
        events = await say(interviews, view.session.id, "Restore the export.")
    assert events[-1] == {"type": "done"}
    assert len(model.calls) == 2, "the third call never happens"
    final = await interviews.view(view.session.id)
    assert final.session.status == "finished"
    assert sorted(o.section for o in final.open_items) == ["contacts", "rollback"]
    usage = FollowUpService(scratch.dsn).usage(view.session.id)
    assert usage["model_calls"] == 2 and usage["completion_reason"] == "model_budget"
    gaps = {
        g["id"]: g for g in GapService(scratch.dsn).queue(world.run_id, OWNER, filter="all")["gaps"]
    }
    assert gaps[rollback_gap]["status"] == "pending"
    assert [a["outcome"] for a in gaps[rollback_gap]["attempts"]] == ["answered_unnoted"]


async def test_failed_provider_attempts_stay_charged_and_replays_are_free(
    scratch: Scratch,
) -> None:
    world, rollback_gap, _ = await triaged_world(scratch)
    plan = prepare(scratch, world, [rollback_gap], limits=FollowUpLimits(model_calls=3))
    seed1 = plan.topics[0].questions[0].text
    model = ScriptedModel(
        [
            tool_reply(ask(seed1, "rollback", rollback_gap)),
            ModelError("provider down"),
            tool_reply(note("rollback", "Restore it."), ("finish_topic", {})),
        ],
        name="interviewer",
    )
    interviews = make_service(scratch.dsn, agent=None, interviewer=model)
    budget = FollowUpService(scratch.dsn)
    with acting_as(OWNER):
        view = await interviews.approve(plan.id, OWNER.name)
        assert budget.usage(view.session.id)["model_calls"] == 1
        failed = await say(interviews, view.session.id, "Restore the export.")
        assert failed[-1]["type"] == "error" and failed[-1]["code"] == "model_unavailable"
        assert budget.usage(view.session.id)["model_calls"] == 2, "a failed attempt stays charged"
        message_id = UUID(failed[-1]["message_id"])
        resumed = [e async for e in await interviews.resume(view.session.id, message_id)]
    assert resumed[-1]["type"] == "done"
    assert budget.usage(view.session.id)["model_calls"] == 3 == len(model.calls)
    with scratch.connect() as conn:
        assert conn.execute(
            "SELECT count(*) FROM interview_replies WHERE session_id=%s", (view.session.id,)
        ).fetchone() == (2,), "only persisted replies exist; the failed call left none"


# --- I4 / I8 / C5: stop, recovery and handover ---


async def test_stop_settles_work_and_returns_gaps_to_triage(scratch: Scratch) -> None:
    world, rollback_gap, request_gap = await triaged_world(scratch)
    plan = prepare(scratch, world, [rollback_gap, request_gap])
    seed1 = plan.topics[0].questions[0].text
    model = ScriptedModel(
        [
            tool_reply(ask(seed1, "rollback", rollback_gap)),
            tool_reply(note("rollback", "Restore it."), ask("More?", "rollback", rollback_gap)),
        ],
        name="interviewer",
    )
    interviews = make_service(scratch.dsn, agent=None, interviewer=model)
    follow = FollowUpService(scratch.dsn)
    with acting_as(OWNER):
        view = await interviews.approve(plan.id, OWNER.name)
        with scratch.connect() as conn, pytest.raises(ConflictError, match="follow-up"):
            maintenance.transfer(
                conn,
                world.run_id,
                "samir",
                ADMIN,
                expected_owner=OWNER.subject,
                expected_revision=1,
                reason="Too early",
                start=True,
                identities=(Principal("samir", "Samir"),),
            )
        await say(interviews, view.session.id, "Restore the export.")
        for who in (READER, ADMIN, IMPOSTOR):
            with pytest.raises(NotFoundError):
                follow.stop(view.session.id, who)
        with scratch.connect() as conn:
            conn.execute(
                "UPDATE message_receipts SET status='processing',result=NULL WHERE session_id=%s",
                (view.session.id,),
            )
        with pytest.raises(ConflictError, match="recover"):
            follow.stop(view.session.id, OWNER)
        with scratch.connect() as conn:
            conn.execute(
                "UPDATE message_receipts SET status='processed',result='{}' WHERE session_id=%s",
                (view.session.id,),
            )
        stopped = follow.stop(view.session.id, OWNER)
        assert stopped["completion_reason"] == "expert_stopped"
        assert follow.stop(view.session.id, OWNER) == stopped, "stopping twice is harmless"
        with pytest.raises(ConflictError, match="finished"):
            await say(interviews, view.session.id, "Too late")
    final = await interviews.view(view.session.id)
    assert final.session.status == "finished"
    assert [n.text for n in final.notes] == ["Restore it."], "accepted work is kept"
    assert [(o.section, o.reason) for o in final.open_items] == [
        ("contacts", "the expert stopped the follow-up")
    ]
    gaps = {
        g["id"]: g for g in GapService(scratch.dsn).queue(world.run_id, OWNER, filter="all")["gaps"]
    }
    assert gaps[rollback_gap]["status"] == "awaiting_review"
    assert gaps[request_gap]["status"] == "pending"
    assert [a["outcome"] for a in gaps[request_gap]["attempts"]] == ["unattempted"]
    with scratch.connect() as conn:
        state = maintenance.transfer(
            conn,
            world.run_id,
            "samir",
            ADMIN,
            expected_owner=OWNER.subject,
            expected_revision=1,
            reason="Now the follow-up is settled",
            start=True,
            identities=(Principal("samir", "Samir"),),
        )
    assert state["maintenance_started_at"] is not None


# --- HTTP ---


async def test_http_follow_up_lifecycle(scratch: Scratch) -> None:
    from fastapi.testclient import TestClient

    from eki.api.app import create_app
    from eki.config import Settings

    world, rollback_gap, request_gap = await triaged_world(scratch)
    model = ScriptedModel(
        [tool_reply(ask("placeholder", "rollback", rollback_gap))] * 2, name="interviewer"
    )
    interviews = make_service(scratch.dsn, agent=None, interviewer=model)
    reader_token = "fu-reader-token-00000000000000000000001"
    owner_token = "fu-owner-token-000000000000000000000001"
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
    client = TestClient(create_app(interviews, settings=settings))
    reader = {"Authorization": "Bearer " + reader_token}
    owner = {"Authorization": "Bearer " + owner_token}
    body = {
        "request_id": str(uuid4()),
        "parent_session_id": str(world.session_id),
        "gap_ids": [rollback_gap, request_gap],
        "limits": {"questions": 4},
    }
    assert (
        client.post(f"/runs/{world.run_id}/follow-ups", headers=reader, json=body).status_code
        == 404
    )
    created = client.post(f"/runs/{world.run_id}/follow-ups", headers=owner, json=body)
    assert created.status_code == 201, created.text
    assert (
        client.post(f"/runs/{world.run_id}/follow-ups", headers=owner, json=body).status_code == 200
    )
    plan = created.json()
    assert plan["kind"] == "follow_up" and plan["limits"]["questions"] == 4
    assert plan["topics"][0]["sections"] == ["rollback", "contacts"]
    assert plan["topics"][0]["questions"][0]["gap_id"] == rollback_gap
    plan_id = plan["id"]
    seed1 = plan["topics"][0]["questions"][0]["text"]
    model._replies[:] = [tool_reply(ask(seed1, "rollback", rollback_gap))] * 2
    assert (
        client.post(
            f"/plans/{plan_id}/approve", headers=reader, json={"approved_by": READER.name}
        ).status_code
        == 404
    )
    approved = client.post(
        f"/plans/{plan_id}/approve", headers=owner, json={"approved_by": OWNER.name}
    )
    assert approved.status_code == 200, approved.text
    session = approved.json()
    assert session["kind"] == "follow_up" and session["usage"]["questions"] == 1
    assert session["usage"]["limits"]["questions"] == 4
    assert session["scope"] == {"rollback", "contacts"} or session["scope"] == [
        "rollback",
        "contacts",
    ]
    session_id = session["id"]
    assert client.post(f"/sessions/{session_id}/stop", headers=reader).status_code == 404
    stopped = client.post(f"/sessions/{session_id}/stop", headers=owner)
    assert stopped.status_code == 200, stopped.text
    assert stopped.json()["completion_reason"] == "expert_stopped"
    assert client.get(f"/sessions/{session_id}", headers=owner).json()["status"] == "finished"
