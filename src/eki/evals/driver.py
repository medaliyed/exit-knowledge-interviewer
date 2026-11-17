"""Drive the real interview service in one temporary schema per synthetic scenario."""

import asyncio
import subprocess
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import asdict, replace
from datetime import UTC, datetime, time
from pathlib import Path
from time import perf_counter
from typing import Any
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo

from eki import pool
from eki.audit import AuditRecorder, audit_context
from eki.db import migrate
from eki.evals.budget import BudgetedModel, CallBudget
from eki.evals.expert import SimulatedExpert
from eki.evals.grading import grade
from eki.evals.models import Golden, Report, Scenario, ScenarioResult, load_golden, load_scenario
from eki.graph.workflow import Limits, setup_checkpoints
from eki.ingest.embedding import HashingEmbedder
from eki.ingest.index import index_run
from eki.ingest.pipeline import ingest_from_source, store
from eki.ingest.redaction import redact
from eki.ingest.scope import Scope
from eki.interview.service import InterviewService, SessionView
from eki.llm import ChatModel
from eki.mapping.models import Topic
from eki.mapping.scoring import score_topics
from eki.mapping.store import save_mapping
from eki.mapping.topics import build_topics

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_LIMITS = Limits()
MAIL_FOLDERS = ("Inbox", "Sent", "Vendors")
Pair = tuple[Scenario, Golden]


def load_pairs(scenarios: Path, golden: Path, *, root: Path = ROOT) -> tuple[Pair, ...]:
    """Read human-owned inputs without mutating them; require a complete one-to-one pairing."""
    for directory in (scenarios, golden):
        if not directory.is_dir():
            raise ValueError(f"{directory}: directory does not exist; use --scenarios and --golden")
    found: dict[str, Scenario] = {}
    expected: dict[str, Golden] = {}
    for path in sorted(scenarios.glob("*.json")):
        scenario = load_scenario(path)
        if path.stem != scenario.id or scenario.id in found:
            raise ValueError(f"{path.name}: filename must match a unique scenario id")
        corpus = Path(scenario.corpus)
        if corpus.is_absolute() or not (root / corpus).resolve().is_relative_to(root.resolve()):
            raise ValueError(f"{path.name}: corpus must be relative to the repository root")
        if not (root / corpus).is_dir():
            raise ValueError(f"{path.name}: corpus directory does not exist")
        found[scenario.id] = scenario
    for path in sorted(golden.glob("*.json")):
        item = load_golden(path)
        if path.stem != item.scenario or item.scenario in expected:
            raise ValueError(f"{path.name}: filename must match a unique golden scenario")
        expected[item.scenario] = item
    if found.keys() != expected.keys():
        missing_golden = sorted(found.keys() - expected.keys())
        missing_scenario = sorted(expected.keys() - found.keys())
        raise ValueError(
            f"unpaired files: missing golden {missing_golden}; missing scenario {missing_scenario}"
        )
    if not found:
        raise ValueError(f"{scenarios}: no scenarios found")
    return tuple((found[name], expected[name]) for name in sorted(found))


@contextmanager
def scratch_schema(base_dsn: str) -> Iterator[str]:
    """Migrate and always drop our schema; never reuse public application/checkpoint tables."""
    schema = f"eki_eval_{uuid4().hex}"
    with psycopg.connect(base_dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    dsn = make_conninfo(base_dsn, options=f"-c search_path={schema},public")
    try:
        with psycopg.connect(dsn) as conn:
            # migrate()'s IF NOT EXISTS would otherwise find a public history table.
            conn.execute(
                "CREATE TABLE schema_migrations ("
                "name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
            )
            conn.commit()
            migrate(conn)
        # The checkpointer uses IF NOT EXISTS too. Hide public until its own
        # tables have been created, then let runtime connections see pgvector.
        setup_checkpoints(make_conninfo(base_dsn, options=f"-c search_path={schema}"))
        yield dsn
    finally:
        pool.close(dsn)  # the scenario's services borrowed pooled connections to this schema
        with psycopg.connect(base_dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def select_topic(topics: Sequence[Topic], artifact_ids: Sequence[str]) -> Topic:
    wanted = set(artifact_ids)
    holding = [t for t in topics if len(wanted & set(t.artifact_ids)) >= 0.8 * len(wanted)]
    if not holding:
        raise ValueError("topic not found: no topic holds at least 80% of topic_artifact_ids")
    return min(holding, key=lambda t: (len(t.artifact_ids), t.id))


async def run_scenario(
    scenario: Scenario,
    golden: Golden,
    *,
    dsn: str,
    drafter: ChatModel,
    interviewer: ChatModel,
    max_calls: int = 40,
    limits: Limits = DEFAULT_LIMITS,
    timeout_seconds: float = 60,
    root: Path = ROOT,
) -> ScenarioResult:
    started = perf_counter()
    budget = CallBudget(max_calls)
    view: SessionView | None = None
    topic_id = ""
    error: str | None = None
    domains = frozenset(scenario.expert.work_domains)
    try:
        with scratch_schema(dsn) as isolated:
            run_id = uuid4()
            with audit_context(
                user_id="eval-operator", run_ids=(run_id,), recorder=AuditRecorder(isolated)
            ):
                scope = Scope(
                    expert_name=scenario.expert.name,
                    expert_email=scenario.expert.email,
                    since=datetime.combine(scenario.span.since, time.min, UTC),
                    until=datetime.combine(scenario.span.until, time.max, UTC),
                    work_email_domains=domains,
                    mail_folders=MAIL_FOLDERS,
                )
                corpus = root / scenario.corpus
                snapshot = await ingest_from_source(scope, corpus)
                embedder = HashingEmbedder()
                topics = build_topics(snapshot, embedder)
                topic = select_topic(topics, scenario.topic_artifact_ids)
                topic_id = topic.id
                scores = score_topics(topics, snapshot, as_of=snapshot.until)
                with psycopg.connect(isolated, autocommit=True) as conn:
                    store(conn, snapshot, run_id=run_id)
                    save_mapping(
                        conn, run_id, (topic,), tuple(s for s in scores if s.topic_id == topic_id)
                    )
                    index_run(conn, run_id, embedder)
                service = InterviewService(
                    dsn=isolated,
                    drafter=BudgetedModel(drafter, budget=budget),
                    interviewer=BudgetedModel(interviewer, budget=budget),
                    embedder=embedder,
                    work_email_domains=domains,
                    limits=replace(
                        limits, turns_per_topic=min(limits.turns_per_topic, scenario.question_cap)
                    ),
                    draft_configuration={"timeout_seconds": timeout_seconds, "max_tokens": 4096},
                )
                plan = await service.draft_plan(run_id)
                if len(plan.topics) != 1:
                    raise ValueError("expected exactly one plan topic")
                try:
                    view = await service.approve(plan.id, scenario.expert.name)
                    expert = SimulatedExpert(scenario.script)
                    answered = 0
                    while view.waiting is not None and view.session.status != "finished":
                        if answered >= scenario.question_cap:
                            error = f"question cap of {scenario.question_cap} reached"
                            break
                        events = [
                            event
                            async for event in await service.post(
                                view.session.id, text=expert.answer(view.waiting.section)
                            )
                        ]
                        answered += 1
                        view = await service.view(view.session.id)
                        if any(event.get("type") == "error" for event in events):
                            error = (
                                "ModelError"
                                if any(event.get("code") == "model_unavailable" for event in events)
                                else "AuditError"
                            )
                            break
                finally:
                    # Approval may have started a session before a provider failed.
                    session_id = await service.session_for_plan(plan.id)
                    if session_id is not None:
                        view = await service.view(session_id)
    except Exception as failure:
        # Provider/DB exception strings can contain credentials or a raw prompt.
        error = (
            str(failure)
            if type(failure) is ValueError and str(failure).startswith("topic not found:")
            else type(failure).__name__
        )
    if budget.stopped:
        error = f"model-call budget of {max_calls} calls spent"
    result = (
        grade(view, topic_id, golden)
        if view is not None
        else ScenarioResult(
            scenario=scenario.id, passed=False, finished=False, checks=(), questions=0
        )
    )
    result = result.model_copy(
        update={
            "passed": result.passed and error is None,
            "aborted": budget.stopped or (error is not None and error.startswith("question cap")),
            "error": error,
            "model_calls": budget.calls,
            "input_tokens": budget.input_tokens,
            "output_tokens": budget.output_tokens,
            "seconds": perf_counter() - started,
        }
    )

    # Golden phrases can appear in check details. Redact them too, not only the transcript.
    def clean(value: Any) -> Any:
        if isinstance(value, str):
            return str(redact(value, work_email_domains=domains).text)
        if isinstance(value, dict):
            return {key: clean(item) for key, item in value.items()}
        if isinstance(value, (tuple, list)):
            return [clean(item) for item in value]
        return value

    return ScenarioResult.model_validate(clean(result.model_dump()))


def revision(root: Path = ROOT) -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],  # noqa: S607 - fixed read-only git command
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    return result.stdout.strip() if result.returncode == 0 else None


async def run_all(
    pairs: Sequence[Pair],
    *,
    dsn: str,
    drafter: ChatModel,
    interviewer: ChatModel,
    max_calls: int = 40,
    limits: Limits = DEFAULT_LIMITS,
    timeout_seconds: float = 60,
    root: Path = ROOT,
) -> Report:
    run_at = datetime.now(UTC)
    results = []
    for scenario, golden in pairs:
        results.append(
            await run_scenario(
                scenario,
                golden,
                dsn=dsn,
                drafter=drafter,
                interviewer=interviewer,
                max_calls=max_calls,
                limits=limits,
                timeout_seconds=timeout_seconds,
                root=root,
            )
        )
    return Report(
        run_at=run_at,
        revision=await asyncio.to_thread(revision, root),
        models={
            role: str(redact(model.name, work_email_domains=frozenset()).text)
            for role, model in (("drafter", drafter), ("interviewer", interviewer))
        },
        settings={
            "max_calls": max_calls,
            "limits": asdict(limits),
            "timeout_seconds": timeout_seconds,
            "max_tokens": 4096,
            "mail_folders": MAIL_FOLDERS,
            "question_caps": {s.id: s.question_cap for s, _ in pairs},
        },
        scenarios=tuple(results),
    )
