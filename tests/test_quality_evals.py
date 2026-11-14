"""P4-b: fixed operator questions and expected outcomes, independent of rank scores."""

import pytest

from eki.search.answers import AnswerService
from eki.search.service import SearchService
from tests.conftest import Scratch
from tests.search_support import READER, search_world

pytestmark = [pytest.mark.eval, pytest.mark.db, pytest.mark.anyio]


@pytest.mark.parametrize(
    "query,topic",
    [
        ("month-end reconciliation", "billing"),
        ("restore reconciliation table", "billing"),
        ("replay reconciliation job", "billing"),
        ("gateway certificate", "gateway"),
        ("vendor confirmation", "gateway"),
        ("gateway renewal", "gateway"),
    ],
)
async def test_retrieval_top_one_matches_expected_topic(
    scratch: Scratch, query: str, topic: str
) -> None:
    world = await search_world(scratch)
    hits = SearchService(scratch.dsn).search(READER, query, limit=1)
    assert len(hits) == 1 and hits[0].entry.id == str(getattr(world, topic))
    assert hits[0].entry.status == "approved"


@pytest.mark.parametrize(
    "question,topic,section,phrase",
    [
        ("How do I roll back month-end reconciliation?", "billing", "rollback", "pre-run export"),
        ("How do I run reconciliation replay?", "billing", "steps", "replay flag"),
        ("Who confirms reconciliation totals?", "billing", "contacts", "Finance operations"),
        (
            "How do I perform gateway certificate renewal?",
            "gateway",
            "steps",
            "vendor confirmation",
        ),
    ],
)
async def test_answers_are_relevant_and_fully_cited(
    scratch: Scratch, question: str, topic: str, section: str, phrase: str
) -> None:
    world = await search_world(scratch)
    result = await AnswerService(SearchService(scratch.dsn)).ask(READER, question)
    assert result.status == "answered" and phrase in result.answer
    assert result.evidence and all(
        e.entry_id == str(getattr(world, topic)) and e.section == section and e.citations
        for e in result.evidence
    )


@pytest.mark.parametrize(
    "question",
    [
        "How do I repair a volcano rocket?",
        "Who owns the lunar fleet?",
        "How do I reset payroll passwords?",
        "What database password should I use?",
    ],
)
async def test_unsupported_questions_abstain(scratch: Scratch, question: str) -> None:
    await search_world(scratch)
    answer = await AnswerService(SearchService(scratch.dsn)).ask(READER, question)
    assert answer.status == "insufficient_evidence" and not answer.evidence
