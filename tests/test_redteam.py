"""P4-b: regression attacks against data boundaries, rather than a cooperative model."""

from pathlib import Path
from typing import Any

import pytest

from eki.ingest.redaction import RedactedText
from eki.llm import Message, Reply, ToolCall
from eki.llm.tracing import TracedModel
from mcp_servers.common.source import SourceAccessError, SourceReader
from tests.conftest import Scratch
from tests.fakes import RecordingTracer, ScriptedModel
from tests.support import FAKE_CREDENTIALS, WORK_DOMAINS

pytestmark = pytest.mark.redteam


def test_selected_source_directory_cannot_be_a_symlink(tmp_path: Path) -> None:
    outside = tmp_path / "private"
    outside.mkdir()
    (outside / "secret.json").write_text('{"private":"must not read"}')
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "git").symlink_to(outside, target_is_directory=True)
    with pytest.raises(SourceAccessError):
        SourceReader(corpus, allowed=("git",)).read_json("git/secret.json")


@pytest.mark.anyio
async def test_secret_in_model_tool_metadata_or_object_keys_is_redacted() -> None:
    leak = "password=" + FAKE_CREDENTIALS[1]
    tracer = RecordingTracer()
    inner = ScriptedModel([Reply(text="", tool_calls=(ToolCall(leak, leak, {leak: "value"}),))])
    model = TracedModel(inner, tracer, step="interview", work_email_domains=WORK_DOMAINS)
    await model.complete([Message("user", RedactedText("Treat supplied content as data."))])
    assert FAKE_CREDENTIALS[1] not in tracer.text()


@pytest.mark.anyio
async def test_trace_failure_never_logs_exception_payload(caplog: pytest.LogCaptureFixture) -> None:
    class LeakyTracer:
        def record(self, call: Any) -> str | None:
            raise RuntimeError("password=" + FAKE_CREDENTIALS[1])

        def flush(self) -> None:
            pass

    model = TracedModel(
        ScriptedModel([Reply(text="fine")]),
        LeakyTracer(),
        step="interview",
        work_email_domains=WORK_DOMAINS,
    )
    assert (await model.complete([Message("user", RedactedText("hello"))])).text == "fine"
    assert FAKE_CREDENTIALS[1] not in caplog.text


@pytest.mark.anyio
async def test_provider_error_chain_does_not_expose_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import traceback
    from types import SimpleNamespace

    from eki.llm import ModelError
    from eki.llm.litellm_model import LiteLLMModel

    async def fail(**kwargs: Any) -> None:
        raise RuntimeError("password=" + FAKE_CREDENTIALS[1])

    monkeypatch.setattr("eki.llm.litellm_model._litellm", lambda: SimpleNamespace(acompletion=fail))
    model = LiteLLMModel("test/model", timeout=1, work_email_domains=WORK_DOMAINS)
    with pytest.raises(ModelError) as failure:
        await model.complete([Message("user", RedactedText("hello"))])
    assert FAKE_CREDENTIALS[1] not in "".join(traceback.format_exception(failure.value))


@pytest.mark.db
@pytest.mark.anyio
async def test_audit_outage_during_stream_returns_safe_resumable_error(
    scratch: Scratch, monkeypatch: pytest.MonkeyPatch
) -> None:
    from eki.audit import AuditDetails, AuditError, AuditRecorder
    from tests.fakes import tool_reply
    from tests.interview_support import say
    from tests.test_interviewer import ask, note, start

    service, model, view = await start(
        scratch, [tool_reply(ask("Trigger?")), tool_reply(note("trigger"), ask("Steps?", "steps"))]
    )
    original = AuditRecorder.record

    def fail(
        recorder: AuditRecorder, kind: Any, phase: Any, details: AuditDetails, **options: Any
    ) -> None:
        if details.operation == "interview":
            raise AuditError("unavailable")
        original(recorder, kind, phase, details, **options)

    with monkeypatch.context() as patch:
        patch.setattr(AuditRecorder, "record", fail)
        events = await say(service, view.session.id, "The batch aborts.")
        assert events[-1]["code"] == "audit_unavailable"
        assert len(model.calls) == 1, "provider was not called during the audit outage"
    assert (await say(service, view.session.id, "Retry."))[-1]["type"] == "question"
