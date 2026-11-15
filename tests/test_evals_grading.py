"""F20 (7a): scenario/golden files, the simulated expert, the budget and the pure grader."""

from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from eki.evals.budget import BudgetedModel
from eki.evals.expert import SimulatedExpert
from eki.evals.grading import grade
from eki.evals.models import Golden, Scenario, load_golden, load_scenario
from eki.ingest.redaction import RedactedText
from eki.interview.models import SECTIONS, Note, OpenItem, Session, Turn
from eki.interview.service import SessionView
from eki.llm import Message, ModelError, Reply
from tests.fakes import ScriptedModel

pytestmark = pytest.mark.anyio

TOPIC = "billing-month-end"
SCRIPT: dict[str, Any] = {
    "initial": {
        "trigger": "Month-end totals differ between billing-batch and ledger-db.",
        "diagnosis": "Compare the last posting sequence number on both sides.",
        "steps": "Re-run the reconciliation job with the replay flag.",
        "rollback": None,
        "contacts": "Finance operations, who report it and confirm the totals.",
        "pitfalls": "Run it before the period is closed, or it needs a reopen.",
        "rationale": "Postings in the last minutes of the month land after the cut-off.",
    },
    "follow_ups": {"rollback": "Restore the reconciliation table from the pre-run export."},
    "freeform": None,
}


def scenario_data(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": "G3-month-end",
        "title": "Recurring month-end ticket closed with an unexplained standard remedy",
        "corpus": "tests/fixtures/corpus",
        "expert": {
            "name": "Nadia Ferrand",
            "email": "nadia.ferrand@example.com",
            "work_domains": ["example.com"],
        },
        "span": {"since": "2024-11-01", "until": "2026-10-31"},
        "topic_artifact_ids": ["TCK-0004", "TCK-0014"],
        "script": SCRIPT,
    }
    data.update(overrides)
    return data


def golden_data(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "scenario": "G3-month-end",
        "noted": {
            "trigger": ["month-end"],
            "steps": ["replay flag"],
            "rollback": ["pre-run export"],
            "contacts": ["finance operations"],
        },
        "open": [],
        "follow_ups": ["rollback"],
        "mentions": [],
        "max_questions": 10,
    }
    data.update(overrides)
    return data


# --- files ---


def test_scenario_and_golden_load_with_defaults(tmp_path: Path) -> None:
    scenario = Scenario.model_validate(scenario_data())
    golden = Golden.model_validate(golden_data())
    assert scenario.question_cap == 30
    assert golden.max_unsupported_terms == 2 and golden.min_facts == 1.0
    assert scenario.script.initial["rollback"] is None


def test_unknown_section_or_key_is_rejected_with_the_file_name(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text('{"scenario": "x", "noted": {"summary": ["a"]}, "max_questions": 3}')
    with pytest.raises(ValueError, match=r"bad\.json"):
        load_golden(path)
    path.write_text('{"scenario": "x", "noted": {}, "max_questions": 3, "extra": 1}')
    with pytest.raises(ValueError, match=r"bad\.json"):
        load_golden(path)
    scenario = tmp_path / "s.json"
    scenario.write_text('{"id": "x"}')
    with pytest.raises(ValueError, match=r"s\.json"):
        load_scenario(scenario)


def test_golden_rejects_a_section_both_noted_and_open() -> None:
    with pytest.raises(ValidationError, match="rollback"):
        Golden.model_validate(golden_data(open=["rollback"]))


# --- expert ---


def test_simulated_expert_volunteers_everything_then_answers_follow_ups() -> None:
    expert = SimulatedExpert(Scenario.model_validate(scenario_data()).script)
    first = expert.answer("trigger")
    assert first.startswith("Trigger: Month-end totals")
    assert "Rollback:" not in first
    assert expert.answer("rollback") == "Restore the reconciliation table from the pre-run export."
    assert expert.answer("steps") == "Re-run the reconciliation job with the replay flag."


def test_simulated_expert_with_a_freeform_script_never_volunteers() -> None:
    script = {"initial": dict.fromkeys(SECTIONS), "follow_ups": {}, "freeform": "I don't remember"}
    expert = SimulatedExpert(Scenario.model_validate(scenario_data(script=script)).script)
    assert expert.answer("trigger") == "I don't remember"
    assert expert.answer("steps") == "I don't remember"


# --- budget ---


async def test_budget_passes_n_calls_then_fails_without_calling_the_model() -> None:
    inner = ScriptedModel([Reply(text="one"), Reply(text="two"), Reply(text="never")])
    model = BudgetedModel(inner, max_calls=2)
    messages = [Message(role="user", content=RedactedText("hi"))]
    assert (await model.complete(messages)).text == "one"
    assert (await model.complete(messages)).text == "two"
    with pytest.raises(ModelError, match="budget"):
        await model.complete(messages)
    assert len(inner.calls) == 2 and model.calls == 2 and model.name == inner.name


# --- grading ---


def view(
    *,
    turns: list[Turn],
    notes: list[Note],
    open_items: list[OpenItem] | None = None,
    status: str = "finished",
) -> SessionView:
    session = Session(
        id=uuid4(),
        plan_id=uuid4(),
        run_id=uuid4(),
        status=status,
        current_topic_id=None,
    )
    return SessionView(
        session=session,
        waiting=None,
        checklist={},
        turns=tuple(turns),
        notes=tuple(notes),
        open_items=tuple(open_items or ()),
    )


def question(ordinal: int, section: str, text: str) -> Turn:
    return Turn(
        ordinal=ordinal,
        topic_id=TOPIC,
        speaker="interviewer",
        kind="question",
        text=RedactedText(text),
        section=section,
    )


def answer(ordinal: int, text: str) -> Turn:
    return Turn(
        ordinal=ordinal,
        topic_id=TOPIC,
        speaker="expert",
        kind="answer",
        text=RedactedText(text),
        answers=ordinal - 1,
    )


def note(section: str, text: str, turn: int) -> Note:
    return Note(topic_id=TOPIC, section=section, text=RedactedText(text), turn=turn)


def good_view() -> SessionView:
    first = SimulatedExpert(Scenario.model_validate(scenario_data()).script)
    volunteered = first.answer("trigger")
    turns = [
        question(1, "trigger", "What goes wrong at month end? (TCK-0004: 'totals differ')"),
        answer(2, volunteered),
        question(3, "rollback", "You did not mention how to undo it. How do you roll back?"),
        answer(4, "Restore the reconciliation table from the pre-run export."),
    ]
    notes = [
        note("trigger", "Month-end totals differ between billing-batch and ledger-db.", 2),
        note("diagnosis", "Compare the last posting sequence number on both sides.", 2),
        note("steps", "Re-run the reconciliation job with the replay flag.", 2),
        note("contacts", "Finance operations report it and confirm the totals.", 2),
        note("pitfalls", "Run it before the period is closed, or it needs a reopen.", 2),
        note("rationale", "Postings in the last minutes of the month land after the cut-off.", 2),
        note("rollback", "Restore the reconciliation table from the pre-run export.", 4),
    ]
    return view(turns=turns, notes=notes)


def test_a_complete_interview_passes_every_check() -> None:
    result = grade(good_view(), TOPIC, Golden.model_validate(golden_data()))
    assert result.passed, result
    checks = {c.name: c for c in result.checks}
    assert checks["sections"].found == 4 and checks["sections"].expected == 4
    assert checks["facts"].found == 4 and checks["facts"].expected == 4
    assert checks["follow_ups"].found == 1
    assert checks["invented"].found == 0
    assert checks["questions"].found == 2 and checks["questions"].expected == 10
    assert result.finished


def test_a_missing_follow_up_fails_only_that_check() -> None:
    good = good_view()
    turns = [t for t in good.turns if t.ordinal != 3]
    notes = [n for n in good.notes if n.section != "rollback"]
    result = grade(view(turns=turns, notes=notes), TOPIC, Golden.model_validate(golden_data()))
    failed = {c.name for c in result.checks if not c.passed}
    assert failed == {"follow_ups", "sections", "facts"}
    assert not result.passed


def test_filling_in_a_section_that_must_stay_open_fails() -> None:
    good = good_view()
    golden = Golden.model_validate(
        golden_data(noted={"trigger": ["month-end"]}, follow_ups=[], open=["rollback"])
    )
    open_items = [
        OpenItem(topic_id=TOPIC, section="rollback", reason=RedactedText("not said"), turn=4)
    ]
    filled = grade(
        view(turns=list(good.turns), notes=list(good.notes), open_items=open_items), TOPIC, golden
    )
    assert {c.name for c in filled.checks if not c.passed} == {"open"}
    left_open = grade(
        view(
            turns=list(good.turns),
            notes=[n for n in good.notes if n.section != "rollback"],
            open_items=open_items,
        ),
        TOPIC,
        golden,
    )
    assert left_open.passed


def test_a_note_the_expert_never_said_counts_as_invented() -> None:
    good = good_view()
    invented = note(
        "pitfalls", "Escalate to the Zurich datacentre team within fourteen minutes.", 2
    )
    notes = [n for n in good.notes if n.section != "pitfalls"] + [invented]
    result = grade(
        view(turns=list(good.turns), notes=notes), TOPIC, Golden.model_validate(golden_data())
    )
    checks = {c.name: c for c in result.checks}
    assert checks["invented"].found == 1 and not checks["invented"].passed
    assert checks["invented"].details == (
        "pitfalls: escalate, zurich, datacentre, team, within, fourteen",
    )
    assert {c.name for c in result.checks if not c.passed} == {"invented"}


def test_too_many_questions_fails_the_budget_check() -> None:
    result = grade(good_view(), TOPIC, Golden.model_validate(golden_data(max_questions=1)))
    assert {c.name for c in result.checks if not c.passed} == {"questions"}


def test_a_contradiction_must_be_put_to_the_expert_in_one_question() -> None:
    golden = Golden.model_validate(golden_data(mentions=[["TCK-0004", "TCK-0014"]]))
    missing = grade(good_view(), TOPIC, golden)
    assert {c.name for c in missing.checks if not c.passed} == {"mentions"}
    good = good_view()
    asked = question(5, "steps", "TCK-0004 says replay, TCK-0014 says restore. Which is right?")
    result = grade(view(turns=[*good.turns, asked], notes=list(good.notes)), TOPIC, golden)
    assert result.passed


def test_an_unfinished_session_fails_even_when_every_check_holds() -> None:
    good = good_view()
    result = grade(
        view(turns=list(good.turns), notes=list(good.notes), status="active"),
        TOPIC,
        Golden.model_validate(golden_data()),
    )
    assert not result.passed and not result.finished
    assert all(c.passed for c in result.checks)


def test_other_topics_and_confidential_turns_are_ignored() -> None:
    good = good_view()
    other = Turn(
        ordinal=9,
        topic_id="other",
        speaker="interviewer",
        kind="question",
        text=RedactedText("Unrelated?"),
        section="rollback",
    )
    secret = Turn(
        ordinal=10,
        topic_id=TOPIC,
        speaker="expert",
        kind="answer",
        text=RedactedText("the Zurich team"),
        confidential=True,
        answers=3,
    )
    notes = [*good.notes, note("pitfalls", "Call the Zurich team.", 10)]
    result = grade(
        view(turns=[*good.turns, other, secret], notes=notes),
        TOPIC,
        Golden.model_validate(golden_data()),
    )
    checks = {c.name: c for c in result.checks}
    assert checks["questions"].found == 2
    assert checks["invented"].found == 1, "a confidential turn is never read as support"


async def test_drafter_and_interviewer_share_one_budget_and_record_usage() -> None:
    from eki.evals.budget import CallBudget

    budget = CallBudget(2)
    drafter = ScriptedModel([Reply(text="plan", input_tokens=11, output_tokens=3)])
    interviewer = ScriptedModel([Reply(text="question", input_tokens=20, output_tokens=5)])
    first = BudgetedModel(drafter, budget=budget)
    second = BudgetedModel(interviewer, budget=budget)
    await first.complete([])
    await second.complete([])
    with pytest.raises(ModelError, match="budget"):
        await second.complete([])
    assert budget.stopped and budget.calls == 2
    assert (budget.input_tokens, budget.output_tokens) == (31, 8)
    assert len(drafter.calls) == len(interviewer.calls) == 1
