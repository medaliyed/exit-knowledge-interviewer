"""Deliverable h: the CI workflow runs the same checks as a developer, with pinned inputs.

Plain text checks on purpose: no YAML parser is a project dependency.
"""

import re
from pathlib import Path

import pytest

WORKFLOW = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "ci.yml"
COMMANDS = (
    "uv sync --locked",
    "uv run ruff check .",
    "uv run ruff format --check .",
    "uv run mypy",
    "uv run pytest -q",
    "npm run format:check",
)


@pytest.fixture(scope="module")
def workflow() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


@pytest.mark.parametrize("command", COMMANDS)
def test_runs_command(workflow: str, command: str) -> None:
    assert f"run: {command}" in workflow


def test_actions_pinned_to_commit_sha(workflow: str) -> None:
    uses = re.findall(r"^\s*-?\s*uses:\s*(\S+)", workflow, flags=re.MULTILINE)
    assert len(uses) >= 2
    unpinned = [ref for ref in uses if not re.fullmatch(r"[\w.\-/]+@[0-9a-f]{40}", ref)]
    assert unpinned == []


def test_runner_is_pinned(workflow: str) -> None:
    assert "runs-on: ubuntu-24.04" in workflow
    assert "-latest" not in workflow


def test_least_privilege(workflow: str) -> None:
    assert re.search(r"^permissions:\n\s+contents: read$", workflow, flags=re.MULTILINE)


def test_triggers_on_push_and_pull_request(workflow: str) -> None:
    assert re.search(r"^\s+push:", workflow, flags=re.MULTILINE)
    assert re.search(r"^\s+pull_request:", workflow, flags=re.MULTILINE)


def test_uv_version_is_pinned(workflow: str) -> None:
    assert re.search(r'version:\s*"0\.13\.7"', workflow)


def test_database_tests_run_against_a_pinned_postgres_service(workflow: str) -> None:
    assert re.search(
        r"^\s+services:\n\s+postgres:\n\s+image: pgvector/pgvector:0\.8\.7-pg16$",
        workflow,
        flags=re.MULTILINE,
    )
    assert "run: uv run pytest -q -m db" in workflow
    assert "--health-cmd" in workflow


def test_database_port_is_bound_to_localhost(workflow: str) -> None:
    assert "- 127.0.0.1:5432:5432" in workflow


def test_ci_database_url_points_at_the_service(workflow: str) -> None:
    assert re.search(r"EKI_DATABASE_URL: postgresql://\S+@localhost:5432/eki", workflow)


def test_quality_gate_is_explicit_and_provider_free(workflow: str) -> None:
    assert "run: uv run python evals/run.py --junit reports/quality.xml" in workflow
    assert 'EKI_MODEL_ANSWERER: ""' in workflow
    assert 'EKI_MODEL_EMBEDDING: ""' in workflow
    assert "pytest -m live" not in workflow
