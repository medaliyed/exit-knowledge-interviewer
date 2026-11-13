"""Task P2-i: one trace per model call, redacted, and never in the way of the interview."""

from typing import Any

import pytest

from eki.config import Settings
from eki.ingest.redaction import RedactedText
from eki.llm import Message, ModelError, Reply, ToolCall, ToolSpec
from eki.llm.tracing import LangfuseTracer, ModelCall, NullTracer, TracedModel, build_tracer
from tests.fakes import RecordingTracer, ScriptedModel
from tests.support import FAKE_CREDENTIALS, WORK_DOMAINS

pytestmark = pytest.mark.anyio
DATABASE_URL = "postgresql://u:p@localhost:5432/eki"
IDS = {"run_id": "run-1", "plan_id": "plan-1", "session_id": "session-1"}
TOOL = ToolSpec("ask_expert", "Ask.", {"type": "object", "properties": {}})


def traced(inner: ScriptedModel, tracer: Any, **ids: str | None) -> TracedModel:
    return TracedModel(
        inner, tracer, step="interview", work_email_domains=WORK_DOMAINS, **{**IDS, **ids}
    )


async def test_every_model_call_produces_one_trace() -> None:
    tracer = RecordingTracer()
    call = ToolCall("c1", "ask_expert", {"question": "Why?"})
    inner = ScriptedModel(
        [
            Reply(text="Asking.", tool_calls=(call,), input_tokens=40, output_tokens=9),
            Reply(text="x"),
        ],
        name="provider/model-a",
    )
    model = traced(inner, tracer)
    assert model.name == "provider/model-a"
    reply = await model.complete([Message("user", RedactedText("Start."))], [TOOL])
    await model.complete([Message("user", RedactedText("Again."))])
    assert reply.text == "Asking."
    assert len(tracer.calls) == 2
    first = tracer.calls[0]
    assert (first.step, first.model) == ("interview", "provider/model-a")
    assert (first.run_id, first.plan_id, first.session_id) == ("run-1", "plan-1", "session-1")
    assert [m.content for m in first.messages] == ["Start."]
    assert first.tools == ("ask_expert",)
    assert (first.reply_text, first.tool_calls) == ("Asking.", (call,))
    assert (first.input_tokens, first.output_tokens, first.error) == (40, 9, None)


async def test_a_failed_call_is_traced_and_still_fails() -> None:
    tracer = RecordingTracer()
    model = traced(
        ScriptedModel([ModelError("TimeoutError from model x")]), tracer, session_id=None
    )
    with pytest.raises(ModelError):
        await model.complete([Message("user", RedactedText("Start."))])
    (call,) = tracer.calls
    assert call.error == "ModelError"
    assert (call.reply_text, call.session_id) == (None, None)


async def test_a_tracer_that_raises_does_not_break_the_interview() -> None:
    class Broken:
        def record(self, call: ModelCall) -> str | None:
            raise RuntimeError("langfuse is down")

        def flush(self) -> None:
            raise RuntimeError("langfuse is down")

    model = traced(ScriptedModel([Reply(text="fine")]), Broken())
    assert (await model.complete([Message("user", RedactedText("Start."))])).text == "fine"


async def test_what_is_traced_is_redacted_again() -> None:
    tracer = RecordingTracer()
    leak = f"password={FAKE_CREDENTIALS[1]}"
    call = ToolCall("c1", "record_note", {"section": "steps", "text": f"use {leak}"})
    model = traced(ScriptedModel([Reply(text=f"So {leak}?", tool_calls=(call,))]), tracer)
    await model.complete([Message("user", RedactedText(f"She said {leak}"))])
    assert FAKE_CREDENTIALS[1] not in tracer.text()
    assert tracer.text().count("[REDACTED:credential]") == 3


def test_tracing_is_a_no_op_with_empty_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    built: list[dict[str, Any]] = []

    class FakeLangfuse:
        def __init__(self, **options: Any) -> None:
            built.append(options)

    monkeypatch.setattr("langfuse.Langfuse", FakeLangfuse)
    partial: tuple[tuple[str | None, str | None], ...] = (
        (None, None),
        ("pk", None),
        (None, "sk"),
        ("", "sk"),
        ("pk", ""),
    )
    for public, secret in partial:
        settings = Settings(
            _env_file=None,
            database_url=DATABASE_URL,
            langfuse_public_key=public,
            langfuse_secret_key=secret,
        )
        tracer = build_tracer(settings)
        assert isinstance(tracer, NullTracer)
        assert tracer.record(_call()) is None
        tracer.flush()
    assert built == [], "no client is created without both keys"
    configured = Settings(
        _env_file=None,
        database_url=DATABASE_URL,
        langfuse_host="http://localhost:3000",
        langfuse_public_key="pk-lf-x",
        langfuse_secret_key="sk-lf-y",
    )
    assert isinstance(build_tracer(configured), LangfuseTracer)
    assert built == [
        {"public_key": "pk-lf-x", "secret_key": "sk-lf-y", "host": "http://localhost:3000"}
    ]


def _call(**changes: Any) -> ModelCall:
    fields: dict[str, Any] = {
        "step": "interview",
        "model": "provider/model-a",
        "run_id": "run-1",
        "plan_id": "plan-1",
        "session_id": "session-1",
        "messages": (
            Message("system", RedactedText("Be brief.")),
            Message("user", RedactedText("Hi.")),
        ),
        "tools": ("ask_expert",),
        "reply_text": RedactedText("Hello."),
        "tool_calls": (ToolCall("c1", "ask_expert", {"question": "Why?"}),),
        "input_tokens": 12,
        "output_tokens": 3,
        "error": None,
    }
    return ModelCall(**{**fields, **changes})


def test_langfuse_tracer_sends_one_generation_per_call(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[dict[str, Any]] = []

    class FakeObservation:
        trace_id = "trace-123"

        def __init__(self) -> None:
            self.ended = False

        def end(self) -> None:
            sent[-1]["ended"] = True

    class FakeLangfuse:
        def __init__(self, **options: Any) -> None:
            self.flushes = 0

        def start_observation(self, **options: Any) -> FakeObservation:
            sent.append(options)
            return FakeObservation()

        def flush(self) -> None:
            self.flushes += 1

    monkeypatch.setattr("langfuse.Langfuse", FakeLangfuse)
    tracer = LangfuseTracer(host="http://localhost:3000", public_key="pk", secret_key="sk")
    assert tracer.record(_call()) == "trace-123"
    assert tracer.record(_call(error="ModelError", reply_text=None, tool_calls=())) == "trace-123"
    ok, failed = sent
    assert (ok["name"], ok["as_type"], ok["model"]) == (
        "eki.interview",
        "generation",
        "provider/model-a",
    )
    assert ok["input"] == [
        {"role": "system", "content": "Be brief."},
        {"role": "user", "content": "Hi."},
    ]
    assert ok["output"] == {
        "text": "Hello.",
        "tool_calls": [{"name": "ask_expert", "arguments": {"question": "Why?"}}],
    }
    assert ok["usage_details"] == {"input": 12, "output": 3}
    assert ok["metadata"] == {
        "run_id": "run-1",
        "plan_id": "plan-1",
        "session_id": "session-1",
        "tools": "ask_expert",
    }
    assert ok["ended"] is True
    assert "level" not in ok
    assert (failed["level"], failed["status_message"]) == ("ERROR", "ModelError")
    tracer.flush()
