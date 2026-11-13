"""Deliverable b, live checks. Needs `docker compose up -d`; run with `uv run pytest -m infra`."""

import subprocess
import urllib.request
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
LANGFUSE_HEALTH_URL = "http://localhost:3000/api/public/health"

pytestmark = pytest.mark.infra


def psql(sql: str) -> str:
    """Run one SQL statement in the eki database inside the postgres container."""
    result = subprocess.run(
        [
            "docker",
            "compose",
            "exec",
            "-T",
            "postgres",
            "sh",
            "-c",
            'psql -U "$POSTGRES_USER" -d eki -tAc "$0"',
            sql,
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_pgvector_extension_installed() -> None:
    assert psql("SELECT 1 FROM pg_extension WHERE extname = 'vector'") == "1"


def test_postgres_major_version_is_16() -> None:
    assert psql("SHOW server_version").split(".")[0] == "16"


def test_langfuse_database_exists() -> None:
    assert psql("SELECT 1 FROM pg_database WHERE datname = 'langfuse'") == "1"


def test_langfuse_healthy() -> None:
    with urllib.request.urlopen(LANGFUSE_HEALTH_URL, timeout=10) as response:
        assert response.status == 200


def test_a_trace_reaches_langfuse_and_can_be_read_back() -> None:
    """Task P2-i: a model call traced with the real client shows up in the local Langfuse."""
    import time
    import uuid

    import langfuse

    from eki.config import get_settings
    from eki.ingest.redaction import RedactedText
    from eki.llm import Message
    from eki.llm.tracing import LangfuseTracer, ModelCall, build_tracer

    settings = get_settings()
    tracer = build_tracer(settings)
    assert isinstance(tracer, LangfuseTracer), "EKI_LANGFUSE_* keys must be set in .env"
    marker = f"infra-check-{uuid.uuid4().hex[:10]}"
    trace_id = tracer.record(
        ModelCall(
            step="interview",
            model="test/none",
            run_id="run-infra",
            plan_id="plan-infra",
            session_id=marker,
            messages=(Message("user", RedactedText(f"ping {marker}")),),
            tools=("ask_expert",),
            reply_text=RedactedText("pong"),
            tool_calls=(),
            input_tokens=3,
            output_tokens=1,
            error=None,
        )
    )
    tracer.flush()
    assert trace_id
    secret = settings.langfuse_secret_key
    assert settings.langfuse_public_key and secret is not None
    reader = langfuse.Langfuse(
        public_key=settings.langfuse_public_key,
        secret_key=secret.get_secret_value(),
        host=settings.langfuse_host,
        tracing_enabled=False,
    )
    found = []
    for _ in range(30):
        found = reader.api.observations.get_many(
            trace_id=trace_id, fields="core,basic,io,metadata,model"
        ).data
        if found:
            break
        time.sleep(1)
    assert found, "the trace did not arrive within 30 seconds"
    observation = found[0]
    assert observation.name == "eki.interview"
    assert observation.type == "GENERATION"
    assert marker in str(observation.input)
    assert "run-infra" in str(observation.metadata)
