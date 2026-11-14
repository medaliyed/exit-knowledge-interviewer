"""Tracing of model calls.

``TracedModel`` wraps any ``ChatModel`` and reports each call to a ``Tracer``.
What is reported is redacted once more on the way out, and a tracer that fails
never breaks the call it was reporting. Without Langfuse keys tracing is off.
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from eki.config import Settings
from eki.ingest.redaction import RedactedText, redact
from eki.llm import ChatModel, Message, ModelError, Reply, ToolCall, ToolSpec

logger = logging.getLogger("eki.tracing")


@dataclass(frozen=True)
class ModelCall:
    """One model call, as it is traced. Every text in it is redacted."""

    step: str
    """``draft`` or ``interview``."""
    model: str
    run_id: str | None
    plan_id: str | None
    session_id: str | None
    messages: tuple[Message, ...]
    tools: tuple[str, ...]
    reply_text: RedactedText | None
    tool_calls: tuple[ToolCall, ...]
    input_tokens: int
    output_tokens: int
    error: str | None
    """The type of the failure, when the call failed."""


class Tracer(Protocol):
    def record(self, call: ModelCall) -> str | None:
        """Report one call. Returns the trace ID when there is one."""
        ...

    def flush(self) -> None: ...


class NullTracer:
    """Tracing switched off."""

    def record(self, call: ModelCall) -> str | None:
        return None

    def flush(self) -> None:
        return None


class LangfuseTracer:
    """Sends each model call to Langfuse as one generation in its own trace."""

    def __init__(self, *, host: str, public_key: str, secret_key: str) -> None:
        import langfuse

        self._client = langfuse.Langfuse(public_key=public_key, secret_key=secret_key, host=host)

    def record(self, call: ModelCall) -> str | None:
        import langfuse

        metadata = {
            key: value
            for key, value in (
                ("run_id", call.run_id),
                ("plan_id", call.plan_id),
                ("session_id", call.session_id),
                ("tools", ",".join(call.tools)),
            )
            if value
        }
        options: dict[str, Any] = {
            "name": f"eki.{call.step}",
            "as_type": "generation",
            "model": call.model,
            "input": [{"role": m.role, "content": m.content} for m in call.messages],
            "output": {
                "text": call.reply_text,
                "tool_calls": [{"name": c.name, "arguments": c.arguments} for c in call.tool_calls],
            },
            "usage_details": {"input": call.input_tokens, "output": call.output_tokens},
            "metadata": metadata,
        }
        if call.error:
            options |= {"level": "ERROR", "status_message": call.error}
        with langfuse.propagate_attributes(
            session_id=call.session_id, trace_name=f"eki.{call.step}", metadata=metadata
        ):
            observation = self._client.start_observation(**options)
            observation.end()
        return str(observation.trace_id)

    def flush(self) -> None:
        self._client.flush()


def build_tracer(settings: Settings) -> Tracer:
    """Langfuse when both keys are set, otherwise nothing."""
    public, secret = settings.langfuse_public_key, settings.langfuse_secret_key
    if not public or secret is None or not secret.get_secret_value():
        return NullTracer()
    return LangfuseTracer(
        host=settings.langfuse_host, public_key=public, secret_key=secret.get_secret_value()
    )


def flush_tracer(tracer: Tracer) -> None:
    """Tracing remains best-effort even at CLI shutdown; never log raw SDK failures."""
    try:
        tracer.flush()
    except Exception:
        logger.warning("could not flush model traces")


class TracedModel:
    """A ``ChatModel`` that reports every call, bound to the run and session it works for."""

    def __init__(
        self,
        inner: ChatModel,
        tracer: Tracer,
        *,
        step: str,
        work_email_domains: frozenset[str],
        run_id: str | None = None,
        plan_id: str | None = None,
        session_id: str | None = None,
    ) -> None:
        self._inner = inner
        self._tracer = tracer
        self._step = step
        self._domains = work_email_domains
        self._ids = (run_id, plan_id, session_id)

    @property
    def name(self) -> str:
        return self._inner.name

    async def complete(self, messages: Sequence[Message], tools: Sequence[ToolSpec] = ()) -> Reply:
        try:
            reply = await self._inner.complete(messages, tools)
        except ModelError as error:
            self._report(messages, tools, None, type(error).__name__)
            raise
        self._report(messages, tools, reply, None)
        return reply

    def _clean(self, value: Any) -> Any:
        if isinstance(value, str):
            return redact(value, work_email_domains=self._domains).text
        if isinstance(value, dict):
            return {self._clean(key): self._clean(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._clean(item) for item in value]
        return value

    def _calls(self, calls: Sequence[ToolCall]) -> tuple[ToolCall, ...]:
        return tuple(
            ToolCall(self._clean(c.id), self._clean(c.name), self._clean(c.arguments))
            for c in calls
        )

    def _report(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec],
        reply: Reply | None,
        error: str | None,
    ) -> None:
        run_id, plan_id, session_id = self._ids
        try:
            self._tracer.record(
                ModelCall(
                    step=self._step,
                    model=self._inner.name,
                    run_id=run_id,
                    plan_id=plan_id,
                    session_id=session_id,
                    messages=tuple(
                        Message(
                            m.role,
                            self._clean(m.content),
                            self._calls(m.tool_calls),
                            m.tool_call_id,
                        )
                        for m in messages
                    ),
                    tools=tuple(tool.name for tool in tools),
                    reply_text=None if reply is None else self._clean(reply.text),
                    tool_calls=() if reply is None else self._calls(reply.tool_calls),
                    input_tokens=0 if reply is None else reply.input_tokens,
                    output_tokens=0 if reply is None else reply.output_tokens,
                    error=error,
                )
            )
        except Exception:
            # Tracing is an aid. It must never cost the expert an answer.
            logger.warning("could not record a trace for a %s call", self._step)
