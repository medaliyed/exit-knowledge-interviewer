"""F20 (7c): the owner-review drafts validate and exercise all eight planted topics."""

import pytest

from eki.evals.driver import ROOT, load_pairs, run_all
from tests.conftest import Scratch
from tests.fakes import ScriptedModel, checklist_interviewer
from tests.interview_support import quoting_drafter

DRAFTS = ROOT / "docs/plans/phase-7-drafts"


def test_eight_draft_pairs_validate_without_runtime_work() -> None:
    pairs = load_pairs(DRAFTS / "scenarios", DRAFTS / "golden")
    assert len(pairs) == 8
    assert {s.id.split("-")[0] for s, _ in pairs} == {f"G{n}" for n in range(1, 9)}


@pytest.mark.db
@pytest.mark.anyio
async def test_all_eight_drafts_pass_with_the_scripted_checklist_interviewer(
    scratch: Scratch,
) -> None:
    pairs = load_pairs(DRAFTS / "scenarios", DRAFTS / "golden")
    report = await run_all(
        pairs,
        dsn=scratch.dsn,
        drafter=ScriptedModel(quoting_drafter),
        interviewer=ScriptedModel(checklist_interviewer),
    )
    assert all(s.finished and s.error is None for s in report.scenarios), report
    assert report.passed == 8, report
    first = report.scenarios[0]
    assert all(c.passed for c in first.checks)
    assert any(
        "TCK-0026" in t.text and "TCK-0075" in t.text
        for t in first.transcript
        if t.kind == "question"
    )
    assert len(report.scenarios[6].notes) == 0
    assert len(report.scenarios[6].open_items) == 7
