"""Deliverable f: the generator is deterministic and safe to re-run."""

import hashlib
from pathlib import Path

import pytest

from tests.fixtures.checks import ALL_CHECKS
from tests.fixtures.generator import DEFAULT_SEED, RefusingToOverwriteError, generate
from tests.fixtures.loader import load_corpus

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = REPO_ROOT / "tests" / "fixtures"
GENERATED = ("corpus", "answer_key.json", "expert_answers.json")


def digest(fixtures_dir: Path) -> dict[str, str]:
    """Relative path -> SHA-256 for every generated file under a fixtures directory."""
    files = [fixtures_dir / "answer_key.json", fixtures_dir / "expert_answers.json"]
    files += [path for path in (fixtures_dir / "corpus").rglob("*") if path.is_file()]
    return {
        path.relative_to(fixtures_dir).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(files)
    }


def test_same_seed_gives_identical_output(tmp_path: Path) -> None:
    generate(seed=7, out_dir=tmp_path / "a")
    generate(seed=7, out_dir=tmp_path / "b")
    first, second = digest(tmp_path / "a"), digest(tmp_path / "b")
    assert len(first) == 120 + 80 + 6 + 2 + 1 + 2
    assert first == second


def test_different_seed_gives_different_output(tmp_path: Path) -> None:
    generate(seed=7, out_dir=tmp_path / "a")
    generate(seed=8, out_dir=tmp_path / "b")
    assert digest(tmp_path / "a") != digest(tmp_path / "b")


def test_committed_corpus_matches_default_seed(tmp_path: Path) -> None:
    generate(seed=DEFAULT_SEED, out_dir=tmp_path)
    assert digest(tmp_path) == digest(FIXTURES)


def test_default_seed_is_recorded(tmp_path: Path) -> None:
    generate(out_dir=tmp_path)
    corpus = load_corpus(tmp_path)
    assert corpus.manifest["seed"] == DEFAULT_SEED == corpus.answer_key["seed"]
    assert corpus.manifest["generator"] == "eki-fixtures"


def test_files_are_utf8_with_unix_newlines(tmp_path: Path) -> None:
    generate(seed=7, out_dir=tmp_path)
    for name in digest(tmp_path):
        raw = (tmp_path / name).read_bytes()
        assert b"\r" not in raw, name
        assert raw.endswith(b"\n"), name
        raw.decode("utf-8")


def test_regeneration_removes_stale_files(tmp_path: Path) -> None:
    generate(seed=7, out_dir=tmp_path)
    stale = tmp_path / "corpus" / "tickets" / "EXTRA.json"
    stale.write_text("{}", encoding="utf-8")
    generate(seed=7, out_dir=tmp_path)
    assert not stale.exists()


def test_refuses_to_overwrite_foreign_directory(tmp_path: Path) -> None:
    foreign = tmp_path / "corpus" / "thesis.txt"
    foreign.parent.mkdir()
    foreign.write_text("not a fixture", encoding="utf-8")
    with pytest.raises(RefusingToOverwriteError):
        generate(seed=7, out_dir=tmp_path)
    assert foreign.read_text(encoding="utf-8") == "not a fixture"
    assert not (tmp_path / "answer_key.json").exists()


def test_only_the_generated_paths_are_touched(tmp_path: Path) -> None:
    neighbour = tmp_path / "loader.py"
    neighbour.write_text("# keep me\n", encoding="utf-8")
    generate(seed=7, out_dir=tmp_path)
    assert neighbour.read_text(encoding="utf-8") == "# keep me\n"
    assert sorted(path.name for path in tmp_path.iterdir()) == sorted([*GENERATED, "loader.py"])


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_plants_hold_for_other_seeds(tmp_path: Path, seed: int) -> None:
    generate(seed=seed, out_dir=tmp_path)
    corpus = load_corpus(tmp_path)
    for check in ALL_CHECKS:
        check(corpus)
