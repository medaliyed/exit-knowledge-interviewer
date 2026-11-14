"""F11: useful cited answers, abstention, model grounding and in-flight revocation."""

import json
from uuid import UUID

import pytest

from eki import access
from eki.llm import ModelError, Reply, ToolCall
from eki.runbook.models import SECTION_NAMES
from eki.runbook.review import SectionEdit
from eki.runbook.service import RunbookService
from eki.search.answers import AnswerService
from eki.search.service import SearchService
from tests.conftest import Scratch
from tests.fakes import ScriptedModel
from tests.search_support import OWNER, READER, STRANGER, search_world
from tests.support import FAKE_CREDENTIALS

pytestmark = [pytest.mark.db, pytest.mark.anyio]


async def test_useful_exact_claim_answer_and_abstention(scratch: Scratch) -> None:
    world = await search_world(scratch)
    service = AnswerService(SearchService(scratch.dsn))
    answer = await service.ask(READER, "How do I roll back month-end reconciliation?")
    assert answer.status == "answered"
    assert "Restore the reconciliation table from the pre-run export" in answer.answer
    assert answer.evidence and all(UUID(e.entry_id) == world.billing for e in answer.evidence)
    assert all(e.section == "rollback" and e.citations for e in answer.evidence)
    unknown = await service.ask(READER, "How do I repair a volcano rocket?")
    assert unknown.status == "insufficient_evidence" and not unknown.evidence
    assert (
        await service.ask(STRANGER, "reconciliation rollback")
    ).status == "insufficient_evidence"


async def test_only_open_questions_produce_no_procedural_answer(scratch: Scratch) -> None:
    world = await search_world(scratch)
    books = RunbookService(scratch.dsn)
    books.edit(
        world.billing,
        OWNER.name,
        {s: SectionEdit(open_question="I don't remember reconciliation") for s in SECTION_NAMES},
    )
    books.approve(world.billing, OWNER.name)
    result = await AnswerService(SearchService(scratch.dsn)).ask(READER, "reconciliation")
    assert result.status == "insufficient_evidence" and not result.evidence
    assert result.open_questions


@pytest.mark.parametrize(
    "reply",
    [
        Reply(text='{"evidence_ids":["forged"],"abstain":false}'),
        Reply(text='{"evidence_ids":["E1"],"abstain":false,"answer":"invented prose"}'),
        Reply(text="not json"),
        Reply(
            text='{"evidence_ids":["E1"],"abstain":false}',
            tool_calls=(ToolCall("x", "publish", {}),),
        ),
    ],
)
async def test_selector_cannot_forge_citations_or_write_answer_prose(
    scratch: Scratch, reply: Reply
) -> None:
    await search_world(scratch)
    model = ScriptedModel([reply])
    service = AnswerService(SearchService(scratch.dsn), model)
    with pytest.raises(ModelError):
        await service.ask(READER, "reconciliation rollback")


async def test_query_redaction_and_revocation_during_model_call(scratch: Scratch) -> None:
    world = await search_world(scratch)

    def select(messages: object, tools: object) -> Reply:
        with scratch.connect() as conn:
            access.set_readers(conn, world.run_id, [], OWNER)
        return Reply(text=json.dumps({"evidence_ids": ["E1"], "abstain": False}))

    model = ScriptedModel(select)
    service = AnswerService(SearchService(scratch.dsn), model)
    result = await service.ask(READER, f"reconciliation rollback password={FAKE_CREDENTIALS[1]}")
    assert result.status == "insufficient_evidence" and not result.evidence
    assert len(model.calls) == 1
    assert FAKE_CREDENTIALS[1] not in str(model.calls)
    assert model.calls[0].tools == ()


async def test_selected_procedure_keeps_prerequisites_and_step_order(scratch: Scratch) -> None:
    world = await search_world(scratch)
    books = RunbookService(scratch.dsn)
    books.edit(
        world.billing,
        OWNER.name,
        {
            "steps": SectionEdit(
                claims=(
                    "Confirm the reconciliation period is still open.",
                    "Run the reconciliation replay job.",
                    "Verify the totals before closing the period.",
                )
            )
        },
    )
    books.approve(world.billing, OWNER.name)

    def select(messages: object, tools: object) -> Reply:
        # A selector may prefer the middle action; code must still include the full procedure.
        from collections.abc import Sequence
        from typing import cast

        from eki.llm import Message

        payload = json.loads(cast(Sequence[Message], messages)[1].content)
        middle = next(
            e["id"]
            for e in payload["evidence"]
            if e["text"] == "Run the reconciliation replay job."
        )
        return Reply(text=json.dumps({"evidence_ids": [middle], "abstain": False}))

    answer = await AnswerService(SearchService(scratch.dsn), ScriptedModel(select)).ask(
        READER, "How do I run reconciliation replay?"
    )
    assert [e.text for e in answer.evidence] == [
        "Confirm the reconciliation period is still open.",
        "Run the reconciliation replay job.",
        "Verify the totals before closing the period.",
    ]


async def test_answer_api_returns_sources_and_handles_provider_failure(scratch: Scratch) -> None:
    from fastapi.testclient import TestClient

    from eki.api.app import create_app
    from tests.test_api_access import authenticator, headers

    await search_world(scratch)
    service = AnswerService(SearchService(scratch.dsn))
    client = TestClient(create_app(answer_service=service, authenticator=authenticator()))
    result = client.post(
        "/runbook/ask",
        json={"question": "How do I roll back reconciliation?"},
        headers=headers("reader"),
    )
    assert result.status_code == 200 and result.json()["evidence"]
    assert client.post("/runbook/ask", json={"question": "x"}).status_code == 401
    assert (
        client.post("/runbook/ask", json={"question": " "}, headers=headers("reader")).status_code
        == 422
    )
    service.model = ScriptedModel([ModelError("unavailable")])
    assert (
        client.post(
            "/runbook/ask", json={"question": "reconciliation rollback"}, headers=headers("reader")
        ).status_code
        == 502
    )


async def test_weak_retrieval_does_not_force_an_unrelated_answer(
    scratch: Scratch, monkeypatch: pytest.MonkeyPatch
) -> None:
    await search_world(scratch)
    search = SearchService(scratch.dsn)
    irrelevant = search.search(READER, "gateway certificate")
    # Simulate a vector false positive. Retrieval alone is not proof of answer relevance.
    monkeypatch.setattr(search, "search", lambda *args, **kwargs: irrelevant)
    answer = await AnswerService(search).ask(READER, "How do I repair a volcano rocket?")
    assert answer.status == "insufficient_evidence" and not answer.evidence


async def test_edit_during_model_call_suppresses_old_answer(scratch: Scratch) -> None:
    world = await search_world(scratch)
    books = RunbookService(scratch.dsn)

    def select(messages: object, tools: object) -> Reply:
        books.edit(
            world.billing, OWNER.name, {"rollback": SectionEdit(claims=("Changed rollback",))}
        )
        books.approve(world.billing, OWNER.name)
        return Reply(text='{"evidence_ids":["E1"],"abstain":false}')

    answer = await AnswerService(SearchService(scratch.dsn), ScriptedModel(select)).ask(
        READER, "reconciliation rollback"
    )
    assert answer.status == "insufficient_evidence" and not answer.evidence


async def test_denied_run_never_enters_answer_model_context(scratch: Scratch) -> None:
    await search_world(scratch)
    hidden = await search_world(scratch)
    books = RunbookService(scratch.dsn)
    marker = "Denied run information must not reach the model"
    books.edit(hidden.billing, OWNER.name, {"rollback": SectionEdit(claims=(marker,))})
    books.approve(hidden.billing, OWNER.name)
    with scratch.connect() as conn:
        access.set_readers(conn, hidden.run_id, [], OWNER)
    model = ScriptedModel([Reply(text='{"evidence_ids":["E1"],"abstain":false}')])
    answer = await AnswerService(SearchService(scratch.dsn), model).ask(
        READER, "reconciliation rollback"
    )
    assert answer.status == "answered"
    assert marker not in str(model.calls)
    assert str(hidden.billing) not in str(model.calls)
    assert marker not in answer.model_dump_json()
