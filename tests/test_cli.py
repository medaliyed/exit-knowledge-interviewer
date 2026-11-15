"""Deliverable j: the eki command line runs the phase 1 pipeline end to end. Needs Postgres."""

import json
import uuid
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import quote

import pytest
from psycopg import sql

from eki.cli import main
from eki.config import get_settings
from eki.db import connect
from tests.conftest import Scratch
from tests.support import CORPUS, EXPERT_EMAIL, EXPERT_NAME, FAKE_CREDENTIALS

pytestmark = pytest.mark.db

RANGE = ["--since", "2024-11-01", "--until", "2026-10-31"]


@pytest.fixture
def scratch_database(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Point the CLI at a throwaway schema through the connection URL, then drop it."""
    get_settings.cache_clear()
    base_url = get_settings().database_url.get_secret_value()
    schema = f"test_{uuid.uuid4().hex[:12]}"
    admin = connect(get_settings())
    admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    admin.commit()
    separator = "&" if "?" in base_url else "?"
    options = quote(f"-csearch_path={schema},public")
    monkeypatch.setenv("EKI_DATABASE_URL", f"{base_url}{separator}options={options}")
    monkeypatch.setenv("EKI_EXPERT_NAME", EXPERT_NAME)
    monkeypatch.setenv("EKI_EXPERT_EMAIL", EXPERT_EMAIL)
    monkeypatch.setenv("EKI_WORK_EMAIL_DOMAINS", "example.com")
    monkeypatch.setenv("EKI_SOURCE_DIR", str(CORPUS))
    get_settings.cache_clear()
    try:
        yield
    finally:
        admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
        admin.commit()
        admin.close()
        get_settings.cache_clear()


def ingest(capsys: pytest.CaptureFixture[str]) -> str:
    assert main(["migrate"]) == 0
    capsys.readouterr()
    assert main(["ingest", *RANGE]) == 0
    run_id = capsys.readouterr().out.strip().splitlines()[-1]
    uuid.UUID(run_id)
    return run_id


def test_cli_end_to_end(scratch_database: None, capsys: pytest.CaptureFixture[str]) -> None:
    run_id = ingest(capsys)

    assert main(["map", "--run", run_id]) == 0
    assert "topics" in capsys.readouterr().out

    assert main(["gaps", "--run", run_id, "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert len(rows) > 20
    scores = [row["score"] for row in rows]
    assert scores == sorted(scores, reverse=True)
    assert set(rows[0]) >= {
        "rank",
        "topic_id",
        "kind",
        "system",
        "label",
        "score",
        "exclusivity",
        "doc_gap",
        "recency",
        "criticality",
        "doc_status",
        "linked_documents",
        "explanation",
        "artifact_count",
    }
    assert rows[0]["rank"] == 1
    top_ten = {row["topic_id"] for row in rows[:10]}
    assert "system:report-export:report-export" in top_ten

    assert main(["gaps", "--run", run_id, "--limit", "5"]) == 0
    table = capsys.readouterr().out
    assert len([line for line in table.splitlines() if line[:4].strip().isdigit()]) == 5
    assert "report-export" in table
    for secret in FAKE_CREDENTIALS:
        assert secret not in table
        assert secret not in json.dumps(rows)


def test_ingest_indexes_chunks(scratch_database: None, capsys: pytest.CaptureFixture[str]) -> None:
    run_id = ingest(capsys)
    with connect(get_settings()) as conn:
        chunks = conn.execute("SELECT count(*) FROM chunks WHERE run_id = %s", (run_id,)).fetchone()
        artifacts = conn.execute(
            "SELECT count(*) FROM artifacts WHERE run_id = %s", (run_id,)
        ).fetchone()
    assert chunks is not None and artifacts is not None
    assert chunks[0] >= artifacts[0] > 200


def test_migrate_twice_is_fine(scratch_database: None, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["migrate"]) == 0
    assert "0001_init.sql" in capsys.readouterr().out
    assert main(["migrate"]) == 0
    assert "up to date" in capsys.readouterr().out.lower()


def test_gaps_for_unknown_run_exits_non_zero(
    scratch_database: None, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["migrate"]) == 0
    assert main(["gaps", "--run", str(uuid.uuid4())]) == 1
    assert "no completed run" in capsys.readouterr().err.lower()


def test_map_for_unknown_run_exits_non_zero(
    scratch_database: None, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["migrate"]) == 0
    assert main(["map", "--run", str(uuid.uuid4())]) == 1
    assert "no completed run" in capsys.readouterr().err.lower()


def test_gaps_before_map_says_what_to_do(
    scratch_database: None, capsys: pytest.CaptureFixture[str]
) -> None:
    run_id = ingest(capsys)
    assert main(["gaps", "--run", run_id]) == 1
    assert "eki map" in capsys.readouterr().err


def test_ingest_without_an_expert_is_refused(
    scratch_database: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("EKI_EXPERT_NAME", "")
    monkeypatch.setenv("EKI_EXPERT_EMAIL", "")
    get_settings.cache_clear()
    assert main(["ingest", *RANGE]) == 2
    assert "expert" in capsys.readouterr().err.lower()


def test_a_malformed_run_id_is_a_usage_error(scratch_database: None) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["gaps", "--run", "not-a-uuid"])
    assert excinfo.value.code == 2


def test_a_reversed_date_range_is_refused(
    scratch_database: None, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["ingest", "--since", "2026-01-01", "--until", "2025-01-01"]) == 2
    assert "before" in capsys.readouterr().err.lower()


# --- phase 2: plan and interview ---


def use_fake_models(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the CLI build its service with rule-following fakes instead of a real model."""
    from eki.config import Settings
    from eki.graph.workflow import Limits
    from eki.interview.service import InterviewService
    from tests.fakes import ScriptedModel, checklist_interviewer
    from tests.interview_support import make_service, quoting_drafter

    def build(settings: Settings) -> InterviewService:
        return make_service(
            settings.database_url.get_secret_value(),
            agent=None,
            drafter=ScriptedModel(quoting_drafter, name="drafter"),
            interviewer=ScriptedModel(checklist_interviewer, name="interviewer"),
            limits=Limits(plan_size=2),
        )

    monkeypatch.setattr("eki.cli.build_service", build)


def typed(monkeypatch: pytest.MonkeyPatch, *lines: str) -> None:
    """Feed lines to the interview prompt; the input ends after the last one."""
    remaining = list(lines)

    def read(prompt: str = "") -> str:
        if not remaining:
            raise EOFError
        return remaining.pop(0)

    monkeypatch.setattr("builtins.input", read)


def planned(capsys: pytest.CaptureFixture[str]) -> tuple[str, str]:
    """Ingest, map and plan. Returns the plan ID and what the plan command printed."""
    run_id = ingest(capsys)
    assert main(["map", "--run", run_id]) == 0
    capsys.readouterr()
    assert main(["plan", "--run", run_id]) == 0
    out = capsys.readouterr().out
    plan_id = out.strip().splitlines()[-1].split()[-1]
    uuid.UUID(plan_id)
    return plan_id, out


def test_migrate_also_creates_the_checkpoint_tables(scratch_database: None) -> None:
    assert main(["migrate"]) == 0
    with connect(get_settings()) as conn:
        assert conn.execute("SELECT count(*) FROM checkpoints").fetchone() == (0,)


def test_plan_shows_questions_with_their_quotes(
    scratch_database: None, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    use_fake_models(monkeypatch)
    plan_id, out = planned(capsys)
    # Two by score, plus two more whose tickets contradict each other on the restart order.
    assert f"Plan {plan_id} (draft), 4 topics" in out
    assert "its sources contradict each other" in out
    assert "Which is right?" in out
    assert out.count("       Q: ") >= 2
    assert out.count("          from ") >= 2
    assert "gap score" in out
    assert f"eki interview --plan {plan_id}" in out
    for secret in FAKE_CREDENTIALS:
        assert secret not in out


def test_interview_in_the_terminal_with_quit_and_resume(
    scratch_database: None, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    use_fake_models(monkeypatch)
    plan_id, _ = planned(capsys)
    typed(
        monkeypatch,
        "/skip",
        "/confidential Ask Marek, privately.",
        "",
        "Restart it.",
        "/correct x",
        "/correct 6 Restart it twice.",
        "/quit",
    )
    assert main(["interview", "--plan", plan_id]) == 0
    first = capsys.readouterr().out
    assert f"Plan approved by {EXPERT_NAME}." in first
    assert "--- Topic" not in first.split("Plan approved")[0]
    assert "left open trigger: skipped by expert" in first
    assert "noted diagnosis: confidential" in first
    assert "Marek" not in first, "a confidential answer is not echoed"
    assert "noted steps: Restart it." in first
    assert "usage: /correct TURN NEW-TEXT" in first
    assert "turn 6 corrected (steps updated)" in first
    assert f"Stopped. Continue with: eki interview --plan {plan_id}" in first

    typed(monkeypatch, *["I don't know."] * 7)
    assert main(["interview", "--plan", plan_id]) == 0
    second = capsys.readouterr().out
    assert "Plan approved" not in second, "the second run resumes; it does not approve again"
    assert "[turn 7, rollback]" in second, "it continues at the question that was waiting"
    assert "--- Topic 2 of 4:" in second
    assert "Interview finished: 2 sections noted, 26 left open." in second

    typed(monkeypatch)
    assert main(["interview", "--plan", plan_id]) == 0
    assert "Interview finished" in capsys.readouterr().out


def test_end_of_input_stops_like_quit(
    scratch_database: None, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    use_fake_models(monkeypatch)
    plan_id, _ = planned(capsys)
    typed(monkeypatch)
    assert main(["interview", "--plan", plan_id, "--approve-as", "Someone Else"]) == 0
    out = capsys.readouterr().out
    assert "Plan approved by Someone Else." in out
    assert "Stopped. Continue with" in out


def test_plan_and_interview_errors(
    scratch_database: None, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    use_fake_models(monkeypatch)
    assert main(["migrate"]) == 0
    assert main(["plan", "--run", str(uuid.uuid4())]) == 1
    assert "no completed run" in capsys.readouterr().err
    assert main(["interview", "--plan", str(uuid.uuid4())]) == 1
    assert "no plan" in capsys.readouterr().err
    with pytest.raises(SystemExit) as excinfo:
        main(["interview", "--plan", "not-a-uuid"])
    assert excinfo.value.code == 2


def test_plan_without_a_model_name_is_a_usage_error(
    scratch_database: None, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EKI_MODEL_DRAFTER", "")
    get_settings.cache_clear()
    assert main(["plan", "--run", str(uuid.uuid4())]) == 2
    assert "EKI_MODEL_DRAFTER is not set" in capsys.readouterr().err


# --- phase 3a: entries and resumable review, without model configuration ---


def test_entries_and_review_quit_resume(
    scratch: Scratch, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import asyncio

    from eki.interview import store as interviews
    from eki.runbook.service import RunbookService
    from tests.test_workflow_synthesis import finished

    session = asyncio.run(finished(scratch))
    with scratch.connect() as conn:
        plan = interviews.load_session(conn, session)
        assert plan is not None
    monkeypatch.setenv("EKI_DATABASE_URL", scratch.dsn)
    monkeypatch.setenv("EKI_MODEL_DRAFTER", "")
    monkeypatch.setenv("EKI_MODEL_INTERVIEWER", "")
    token = "test-review-credential-00000000000001"
    monkeypatch.setenv("EKI_API_TOKEN", token)
    monkeypatch.setenv(
        "EKI_API_TOKENS", json.dumps([{"token": token, "subject": "reviewer", "name": EXPERT_NAME}])
    )
    from eki import access
    from eki.auth import Principal

    with scratch.connect() as conn:
        access.set_owner(conn, plan.run_id, "reviewer", Principal("operator", "Operator", True))
    get_settings.cache_clear()
    try:
        args = ["--plan", str(plan.plan_id)]
        assert main(["entries", *args]) == 0
        listing = capsys.readouterr().out
        assert listing.count("draft") == 3
        assert listing.count("(7 claims to check)") == 3
        typed(monkeypatch, "approve", "reject", "Not needed", "quit")
        assert main(["review", *args]) == 0
        first = capsys.readouterr().out
        assert "Stopped. Continue with: eki review" in first
        assert "interview" in first and "turn 2" in first, "review displays citations"
        assert first.count("      ! not in the cited answer: noted") == 21
        entries = RunbookService(scratch.dsn).list(session)
        assert [e.entry.status for e in entries] == ["approved", "rejected", "draft"]
        typed(monkeypatch, "edit", "steps", "Restart twice.", "", "", "approve")
        assert main(["review", *args]) == 0
        assert "Restart twice." in capsys.readouterr().out
        entries = RunbookService(scratch.dsn).list(session)
        assert [e.entry.status for e in entries] == ["approved", "rejected", "approved"]
        assert main(["entries", *args]) == 0
        lines = capsys.readouterr().out.strip().splitlines()
        assert [line.endswith("(edited by hand)") for line in lines] == [False, False, True]
        assert ["(6 claims to check)" in line for line in lines] == [False, False, True]
        assert ["(7 claims to check)" in line for line in lines] == [True, True, False]
        typed(monkeypatch)
        assert main(["review", *args]) == 0
        assert "No draft entries" in capsys.readouterr().out
    finally:
        get_settings.cache_clear()


def test_search_ask_export_cli_authentication_and_file_safety(
    scratch: Scratch,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    import asyncio

    from tests.search_support import search_world
    from tests.test_api_access import TOKENS

    world = asyncio.run(search_world(scratch))
    monkeypatch.setenv("EKI_DATABASE_URL", scratch.dsn)
    monkeypatch.setenv("EKI_MODEL_EMBEDDING", "")
    monkeypatch.setenv("EKI_MODEL_ANSWERER", "")
    monkeypatch.setenv(
        "EKI_API_TOKENS",
        json.dumps([{"token": TOKENS["reader"], "subject": "reader", "name": "Reader"}]),
    )
    monkeypatch.setenv("EKI_API_TOKEN", TOKENS["reader"])
    get_settings.cache_clear()
    try:
        assert main(["search", "reconciliation", "--json"]) == 0
        assert json.loads(capsys.readouterr().out)["results"][0]["id"] == str(world.billing)
        assert main(["ask", "How do I roll back reconciliation?", "--json"]) == 0
        answer = json.loads(capsys.readouterr().out)
        assert answer["status"] == "answered" and answer["evidence"]
        assert main(["export", "--entry", str(world.billing)]) == 0
        assert "[^source-" in capsys.readouterr().out
        path = tmp_path / "runbook.md"
        assert main(["export", "--run", str(world.run_id), "--output", str(path)]) == 0
        original = path.read_text()
        assert "Unapproved payroll" not in original and "Gateway certificate" in original
        assert main(["export", "--run", str(world.run_id), "--output", str(path)]) == 1
        assert path.read_text() == original
        monkeypatch.setenv("EKI_API_TOKEN", "invalid")
        get_settings.cache_clear()
        assert main(["search", "reconciliation"]) == 2
        assert TOKENS["reader"] not in capsys.readouterr().err
    finally:
        get_settings.cache_clear()


def test_cli_selected_mail_and_audit(
    scratch_database: None, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["migrate"]) == 0
    capsys.readouterr()
    assert (
        main(
            [
                "ingest",
                *RANGE,
                "--mail-folder",
                "Inbox",
                "--mail-folder",
                "Sent",
                "--mail-folder",
                "Vendors",
            ]
        )
        == 0
    )
    run_id = capsys.readouterr().out.strip().splitlines()[-1]
    with connect(get_settings()) as conn:
        assert conn.execute(
            "SELECT mail_folders FROM ingest_runs WHERE id = %s", (run_id,)
        ).fetchone() == (["Inbox", "Sent", "Vendors"],)
        count = conn.execute(
            "SELECT count(*) FROM artifacts WHERE run_id = %s AND kind = 'email'", (run_id,)
        ).fetchone()
        assert count is not None and count[0] > 0
    assert main(["map", "--run", run_id]) == 0
    capsys.readouterr()
    assert main(["audit", "--run", run_id]) == 0
    events = json.loads(capsys.readouterr().out)
    assert all(e["user_id"] == "local-operator" and e["run_id"] == run_id for e in events)
    assert any(e["kind"] == "tool" and e["details"]["operation"] == "list_messages" for e in events)
    assert any(e["kind"] == "ingest" and e["phase"] == "completed" for e in events)
    assert main(["ingest", *RANGE, "--mail-folder", "Personal"]) == 1


@pytest.mark.redteam
def test_failed_ingest_rolls_back_snapshot_and_records_failure(
    scratch_database: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from eki.ingest.pipeline import IngestError

    assert main(["migrate"]) == 0
    capsys.readouterr()

    def fail(*args: object, **kwargs: object) -> int:
        raise IngestError("simulated indexing failure")

    monkeypatch.setattr("eki.cli.index_run", fail)
    assert main(["ingest", *RANGE]) == 1
    with connect(get_settings()) as conn:
        assert conn.execute("SELECT count(*) FROM ingest_runs").fetchone() == (0,)
        phases = conn.execute(
            "SELECT phase FROM audit_events WHERE kind = 'ingest' ORDER BY id"
        ).fetchall()
        assert phases == [("started",), ("failed",)]
