"""F20 (7b): pairing, offline guards, scorecards and isolated real-service driving."""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest
from pytest import CaptureFixture, MonkeyPatch

from eki.config import Settings
from eki.evals import driver
from eki.evals.driver import load_pairs, run_all, run_scenario, scratch_schema, select_topic
from eki.evals.guard import PROVIDER_KEYS
from eki.evals.models import Golden, Report, Scenario, ScenarioResult
from eki.evals.report import compare, render, save
from eki.llm import ModelError, Reply
from evals import grade as cli
from tests.conftest import Scratch
from tests.fakes import ScriptedModel, checklist_interviewer, tool_reply
from tests.interview_support import quoting_drafter
from tests.test_evals_grading import golden_data, scenario_data

pytestmark = pytest.mark.anyio


def pair() -> tuple[Scenario, Golden]:
    return Scenario.model_validate(scenario_data()), Golden.model_validate(golden_data())


def write_pair(tmp_path: Path) -> list[str]:
    scenarios, golden = tmp_path / "scenarios", tmp_path / "golden"
    scenarios.mkdir()
    golden.mkdir()
    scenario, expected = pair()
    (scenarios / f"{scenario.id}.json").write_text(scenario.model_dump_json())
    (golden / f"{scenario.id}.json").write_text(expected.model_dump_json())
    return ["--scenarios", str(scenarios), "--golden", str(golden)]


def report(*, passed: bool = True, calls: int = 3) -> Report:
    return Report(
        run_at=datetime.now(UTC),
        revision="abc",
        models={"drafter": "fake", "interviewer": "fake"},
        scenarios=(
            ScenarioResult(
                scenario="G3-month-end",
                passed=passed,
                finished=passed,
                checks=(),
                questions=2,
                model_calls=calls,
            ),
        ),
    )


def test_pair_loader_rejects_orphans_in_both_directions(tmp_path: Path) -> None:
    args = write_pair(tmp_path)
    scenarios, golden = Path(args[1]), Path(args[3])
    assert len(load_pairs(scenarios, golden)) == 1
    file = next(golden.glob("*.json"))
    text = file.read_text()
    file.unlink()
    with pytest.raises(ValueError, match="missing golden"):
        load_pairs(scenarios, golden)
    file.write_text(text)
    next(scenarios.glob("*.json")).unlink()
    with pytest.raises(ValueError, match="missing scenario"):
        load_pairs(scenarios, golden)


def test_pair_loader_rejects_mismatched_names_and_external_corpus(tmp_path: Path) -> None:
    args = write_pair(tmp_path)
    scenarios, golden = Path(args[1]), Path(args[3])
    file = next(scenarios.glob("*.json"))
    renamed = file.with_name("wrong.json")
    file.rename(renamed)
    with pytest.raises(ValueError, match=r"wrong\.json"):
        load_pairs(scenarios, golden)
    renamed.rename(file)
    data = json.loads(file.read_text())
    data["corpus"] = "../outside"
    file.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="relative to the repository"):
        load_pairs(scenarios, golden)


def test_dry_run_does_no_settings_database_or_model_work(
    tmp_path: Path, monkeypatch: MonkeyPatch, capsys: CaptureFixture[str]
) -> None:
    args = write_pair(tmp_path)

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("dry-run performed runtime work")

    monkeypatch.setattr(cli, "get_settings", forbidden)
    monkeypatch.setattr(psycopg, "connect", forbidden)
    monkeypatch.setattr(cli, "run_all", forbidden)
    assert cli.main([*args, "--dry-run"]) == 0
    assert "1 scenarios validated" in capsys.readouterr().out
    assert not list(tmp_path.glob("reports/**/*"))
    assert cli.main([*args, "--dry-run", "--only", "absent"]) == 2


def test_env_file_credentials_do_not_opt_in(
    tmp_path: Path, monkeypatch: MonkeyPatch, capsys: CaptureFixture[str]
) -> None:
    args = write_pair(tmp_path)
    for name in PROVIDER_KEYS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("GROQ_API_KEY=must-not-authorize-paid-calls\n")

    def forbidden() -> Settings:
        raise AssertionError("guard must run before settings")

    monkeypatch.setattr(cli, "get_settings", forbidden)
    assert cli.main(args) == 2
    assert "no provider key exported" in capsys.readouterr().err


@pytest.mark.parametrize("missing", ["drafter", "interviewer"])
def test_missing_role_stops_before_runtime(
    missing: str, tmp_path: Path, monkeypatch: MonkeyPatch, capsys: CaptureFixture[str]
) -> None:
    args = write_pair(tmp_path)
    monkeypatch.setenv("GROQ_API_KEY", "synthetic-test-key")
    names: dict[str, Any] = {"model_drafter": "fake", "model_interviewer": "fake"}
    names[f"model_{missing}"] = None
    settings = Settings(_env_file=None, database_url="postgresql://unused", **names)
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    assert cli.main(args) == 2
    assert f"EKI_MODEL_{missing.upper()}" in capsys.readouterr().err


def test_report_round_trip_and_comparison_detect_regression_and_removal(tmp_path: Path) -> None:
    old, new = report(), report(passed=False, calls=4)
    path = tmp_path / "result.json"
    save(old, path)
    assert Report.model_validate_json(path.read_text()) == old
    with pytest.raises(FileExistsError):
        save(new, path)
    changes, regressed = compare(old, new)
    assert regressed and "PASS | FAIL" in changes and "model_calls: 3 -> 4" in changes
    assert "Previous: 1 of 1 passed" in changes
    assert "Current: 0 of 1 passed" in changes
    assert "0 of 1 passed" in render(new)
    assert compare(old, old)[1] is False
    assert compare(old, old.model_copy(update={"scenarios": ()}))[1] is True


def test_cli_comparison_regression_sets_exit_one(
    tmp_path: Path, monkeypatch: MonkeyPatch, capsys: CaptureFixture[str]
) -> None:
    args = write_pair(tmp_path)
    previous = tmp_path / "previous.json"
    save(report(), previous)
    settings = Settings(
        _env_file=None,
        database_url="postgresql://unused",
        model_drafter="fake",
        model_interviewer="fake",
    )
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setenv("GROQ_API_KEY", "synthetic-test-key")

    async def fake_run(*args: Any, **kwargs: Any) -> Report:
        return report().model_copy(update={"scenarios": ()})

    saved: list[Report] = []
    monkeypatch.setattr(cli, "run_all", fake_run)
    monkeypatch.setattr(cli, "save", lambda value, path: saved.append(value))
    assert cli.main([*args, "--compare", str(previous)]) == 1
    assert len(saved) == 1 and "missing" in capsys.readouterr().out


async def test_scenarios_continue_after_an_exception_and_errors_never_echo_secrets(
    monkeypatch: MonkeyPatch,
) -> None:
    @contextmanager
    def unavailable(dsn: str) -> Iterator[str]:
        raise RuntimeError("database-password-must-not-appear")
        yield dsn

    monkeypatch.setattr(driver, "scratch_schema", unavailable)
    output = await run_all(
        [pair(), pair()], dsn="unused", drafter=ScriptedModel([]), interviewer=ScriptedModel([])
    )
    assert len(output.scenarios) == 2
    assert all(s.error == "RuntimeError" for s in output.scenarios)
    assert "database-password" not in output.model_dump_json()
    assert output.settings["max_calls"] == 40


def test_missing_topic_has_an_explicit_reason() -> None:
    with pytest.raises(ValueError, match="topic not found"):
        select_topic([], ["TCK-missing"])


@pytest.mark.db
async def test_driver_runs_real_service_to_seven_notes_and_drops_its_schema(
    scratch: Scratch,
) -> None:
    with scratch.connect() as conn:
        before = conn.execute(
            "SELECT nspname FROM pg_namespace WHERE nspname LIKE 'eki_eval_%'"
        ).fetchall()
    output = await run_all(
        [pair()],
        dsn=scratch.dsn,
        drafter=ScriptedModel(quoting_drafter),
        interviewer=ScriptedModel(checklist_interviewer),
    )
    result = output.scenarios[0]
    assert result.error is None, result
    assert result.finished and result.passed and len(result.notes) == 7, result
    assert result.model_calls > 0 and result.transcript
    with scratch.connect() as conn:
        assert (
            conn.execute(
                "SELECT nspname FROM pg_namespace WHERE nspname LIKE 'eki_eval_%'"
            ).fetchall()
            == before
        )
        assert conn.execute("SELECT count(*) FROM ingest_runs").fetchone() == (0,)


@pytest.mark.db
async def test_driver_budget_stops_during_drafting_and_during_interview(scratch: Scratch) -> None:
    for max_calls in (1, 2):
        scenario, golden = pair()
        result = await run_scenario(
            scenario,
            golden,
            dsn=scratch.dsn,
            drafter=ScriptedModel(quoting_drafter),
            interviewer=ScriptedModel(checklist_interviewer),
            max_calls=max_calls,
        )
        assert result.aborted and not result.passed and "budget" in (result.error or ""), result
        assert result.model_calls == max_calls
        if max_calls == 2:
            assert len(result.transcript) >= 2
    drafting = await run_scenario(
        *pair(),
        dsn=scratch.dsn,
        drafter=ScriptedModel([Reply(text="invalid JSON")]),
        interviewer=ScriptedModel([]),
        max_calls=1,
    )
    assert drafting.aborted and drafting.model_calls == 1 and not drafting.transcript
    failed = await run_scenario(
        *pair(),
        dsn=scratch.dsn,
        drafter=ScriptedModel(quoting_drafter),
        interviewer=ScriptedModel([ModelError("raw-provider-secret")]),
    )
    assert failed.error == "ModelError" and "raw-provider-secret" not in failed.model_dump_json()


@pytest.mark.db
async def test_question_cap_never_asks_an_extra_question(scratch: Scratch) -> None:
    scenario, golden = pair()
    scenario = scenario.model_copy(update={"question_cap": 1})
    model = ScriptedModel(
        lambda messages, tools: tool_reply(
            ("ask_expert", {"question": "More detail?", "section": "trigger"})
        )
    )
    result = await run_scenario(
        scenario, golden, dsn=scratch.dsn, drafter=ScriptedModel(quoting_drafter), interviewer=model
    )
    assert result.questions == 1, result
    assert not result.passed


@pytest.mark.db
def test_scratch_schema_drops_even_if_migrations_fail(
    scratch: Scratch, monkeypatch: MonkeyPatch
) -> None:
    def fail(conn: psycopg.Connection) -> list[str]:
        raise RuntimeError("migration stopped")

    monkeypatch.setattr(driver, "migrate", fail)
    with scratch.connect() as conn:
        before = conn.execute(
            "SELECT nspname FROM pg_namespace WHERE nspname LIKE 'eki_eval_%'"
        ).fetchall()
    with pytest.raises(RuntimeError, match="migration stopped"), scratch_schema(scratch.dsn):
        pytest.fail("migration should have failed")
    with scratch.connect() as conn:
        assert (
            conn.execute(
                "SELECT nspname FROM pg_namespace WHERE nspname LIKE 'eki_eval_%'"
            ).fetchall()
            == before
        )


@pytest.mark.db
async def test_report_redacts_scripted_secrets_and_golden_details(scratch: Scratch) -> None:
    scenario, golden = pair()
    script = scenario.script.model_dump()
    secret = "FAKE-APIKEY-7d1e4c90b2a853f6"
    script["initial"]["trigger"] += " Credential: " + secret
    scenario = Scenario.model_validate({**scenario.model_dump(), "script": script})
    golden = golden.model_copy(update={"noted": {"trigger": (secret,)}})
    result = await run_scenario(
        scenario,
        golden,
        dsn=scratch.dsn,
        drafter=ScriptedModel(quoting_drafter),
        interviewer=ScriptedModel(checklist_interviewer),
    )
    assert result.finished
    assert secret not in result.model_dump_json()
    assert "[REDACTED:credential]" in result.model_dump_json()


@pytest.mark.db
def test_scratch_tables_belong_to_the_temporary_schema(scratch: Scratch) -> None:
    with scratch_schema(scratch.dsn) as dsn, psycopg.connect(dsn) as conn:
        schema = conn.execute("SELECT current_schema()").fetchone()
        assert schema is not None
        for table in (
            "schema_migrations",
            "ingest_runs",
            "checkpoint_migrations",
            "checkpoints",
            "audit_events",
        ):
            owner = conn.execute(
                "SELECT n.nspname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                "WHERE c.oid=%s::regclass",
                (table,),
            ).fetchone()
            assert owner == schema


def test_standalone_dry_run_from_another_directory(tmp_path: Path) -> None:
    import subprocess
    import sys

    args = write_pair(tmp_path)
    result = subprocess.run(
        [sys.executable, str(driver.ROOT / "evals/grade.py"), *args, "--dry-run"],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "1 scenarios validated" in result.stdout
