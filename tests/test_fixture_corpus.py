"""Deliverable f: the committed synthetic corpus contains every planted gap, non-gap and trap."""

from pathlib import Path

import pytest

from tests.fixtures.checks import ALL_CHECKS, Check
from tests.fixtures.loader import LoadedCorpus, load_corpus

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = REPO_ROOT / "tests" / "fixtures"


@pytest.fixture(scope="module")
def corpus() -> LoadedCorpus:
    return load_corpus(FIXTURES)


@pytest.mark.parametrize("check", ALL_CHECKS, ids=lambda check: check.__name__)
def test_committed_corpus(corpus: LoadedCorpus, check: Check) -> None:
    check(corpus)


def test_every_gap_non_gap_and_trap_has_a_check() -> None:
    names = {check.__name__ for check in ALL_CHECKS}
    plants = [f"g{n}" for n in range(1, 9)] + [f"n{n}" for n in range(1, 4)]
    plants += [f"t{n}" for n in range(1, 8)]
    assert {f"check_plant_{plant}" for plant in plants} <= names


def test_pipeline_never_reads_answer_key() -> None:
    forbidden = ("answer_key", "expert_answers")
    offenders = [
        str(path.relative_to(REPO_ROOT))
        for directory in ("src", "mcp_servers")
        for path in sorted((REPO_ROOT / directory).rglob("*"))
        if path.is_file()
        and path.suffix in {".py", ".toml", ".json", ".yaml", ".yml"}
        and any(word in path.read_text(encoding="utf-8") for word in forbidden)
    ]
    assert offenders == []
