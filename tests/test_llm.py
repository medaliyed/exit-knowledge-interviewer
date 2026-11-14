"""Task P2-a: one model interface, a LiteLLM implementation and a scripted fake."""

import json
import re
from types import SimpleNamespace
from typing import Any

import pytest

from eki.config import ConfigError, Settings, model_name
from eki.ingest.redaction import RedactedText
from eki.llm import ChatModel, Message, ModelError, Reply, ToolCall, ToolSpec
from eki.llm.litellm_model import LiteLLMModel
from tests.fakes import ScriptedModel
from tests.support import FAKE_CREDENTIALS, WORK_DOMAINS

pytestmark = pytest.mark.anyio

DATABASE_URL = "postgresql://u:p@localhost:5432/eki"
LOOKUP = ToolSpec(
    name="get_artifact",
    description="Read one artifact.",
    parameters={
        "type": "object",
        "properties": {"artifact_id": {"type": "string"}},
        "required": ["artifact_id"],
    },
)


def text(value: str) -> RedactedText:
    return RedactedText(value)


def provider_response(
    content: str | None = "Hello.", tool_calls: list[Any] | None = None
) -> SimpleNamespace:
    message = SimpleNamespace(content=content, tool_calls=tool_calls)
    usage = SimpleNamespace(prompt_tokens=11, completion_tokens=7)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=usage)


class FakeProvider:
    """Stands in for ``litellm.acompletion``."""

    def __init__(self, response: Any = None, error: Exception | None = None) -> None:
        self.requests: list[dict[str, Any]] = []
        self._response = response if response is not None else provider_response()
        self._error = error

    async def __call__(self, **request: Any) -> Any:
        self.requests.append(request)
        if self._error is not None:
            raise self._error
        return self._response


def litellm_model(monkeypatch: pytest.MonkeyPatch, provider: FakeProvider) -> LiteLLMModel:
    monkeypatch.setattr("litellm.acompletion", provider)
    return LiteLLMModel("provider/some-model", timeout=12.5, work_email_domains=WORK_DOMAINS)


async def test_scripted_model_replays_in_order() -> None:
    model: ChatModel = ScriptedModel([Reply(text="one"), Reply(text="two")])
    assert (await model.complete([Message("user", text("a"))])).text == "one"
    assert (await model.complete([Message("user", text("b"))])).text == "two"
    with pytest.raises(AssertionError, match="no reply left"):
        await model.complete([Message("user", text("c"))])


async def test_scripted_model_records_what_it_was_sent() -> None:
    model = ScriptedModel([Reply(text="ok")])
    await model.complete(
        [Message("system", text("be brief")), Message("user", text("hi"))], [LOOKUP]
    )
    (call,) = model.calls
    assert [m.content for m in call.messages] == ["be brief", "hi"]
    assert call.tools == (LOOKUP,)
    assert "be brief" in model.sent_text()


async def test_scripted_model_can_raise_and_follow_a_policy() -> None:
    failing = ScriptedModel([ModelError("down")])
    with pytest.raises(ModelError):
        await failing.complete([Message("user", text("a"))])
    echo = ScriptedModel(lambda messages, tools: Reply(text=messages[-1].content.upper()))
    assert (await echo.complete([Message("user", text("abc"))])).text == "ABC"


async def test_litellm_model_maps_messages_and_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = FakeProvider()
    model = litellm_model(monkeypatch, provider)
    call = ToolCall(id="call-1", name="get_artifact", arguments={"artifact_id": "TCK-0001"})
    await model.complete(
        [
            Message("system", text("You interview an expert.")),
            Message("user", text("Start.")),
            Message("assistant", text(""), tool_calls=(call,)),
            Message("tool", text("ticket body"), tool_call_id="call-1"),
        ],
        [LOOKUP],
    )
    (request,) = provider.requests
    assert request["model"] == "provider/some-model"
    assert request["timeout"] == 12.5
    assert request["messages"][:2] == [
        {"role": "system", "content": "You interview an expert."},
        {"role": "user", "content": "Start."},
    ]
    assistant, tool = request["messages"][2:]
    assert assistant["role"] == "assistant"
    assert assistant["tool_calls"] == [
        {
            "id": "call-1",
            "type": "function",
            "function": {"name": "get_artifact", "arguments": '{"artifact_id": "TCK-0001"}'},
        }
    ]
    assert tool == {"role": "tool", "tool_call_id": "call-1", "content": "ticket body"}
    assert request["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "get_artifact",
                "description": "Read one artifact.",
                "parameters": LOOKUP.parameters,
            },
        }
    ]


async def test_litellm_model_sends_no_tools_key_without_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = FakeProvider()
    await litellm_model(monkeypatch, provider).complete([Message("user", text("hi"))])
    assert "tools" not in provider.requests[0]


async def test_litellm_model_maps_tool_calls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = SimpleNamespace(
        id="call-9",
        function=SimpleNamespace(name="ask_expert", arguments=json.dumps({"question": "Why?"})),
    )
    broken = SimpleNamespace(id="call-10", function=SimpleNamespace(name="x", arguments="{oops"))
    provider = FakeProvider(provider_response(content=None, tool_calls=[raw, broken]))
    reply = await litellm_model(monkeypatch, provider).complete([Message("user", text("hi"))])
    assert reply.text == ""
    assert reply.tool_calls == (
        ToolCall(id="call-9", name="ask_expert", arguments={"question": "Why?"}),
        ToolCall(id="call-10", name="x", arguments={}),
    )
    assert (reply.input_tokens, reply.output_tokens) == (11, 7)


async def test_provider_error_becomes_model_error(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = FakeProvider(error=TimeoutError("upstream said: secret-looking detail"))
    with pytest.raises(ModelError) as excinfo:
        await litellm_model(monkeypatch, provider).complete([Message("user", text("hi"))])
    assert "TimeoutError" in str(excinfo.value)
    assert "provider/some-model" in str(excinfo.value)
    assert "secret-looking detail" not in str(excinfo.value)


async def test_empty_provider_response_becomes_model_error(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = FakeProvider(SimpleNamespace(choices=[], usage=None))
    with pytest.raises(ModelError):
        await litellm_model(monkeypatch, provider).complete([Message("user", text("hi"))])


async def test_message_content_must_be_redacted_text(monkeypatch: pytest.MonkeyPatch) -> None:
    """The type says RedactedText; the runtime guard redacts again at the model boundary."""
    provider = FakeProvider()
    model = litellm_model(monkeypatch, provider)
    leaked = f"password={FAKE_CREDENTIALS[1]} and mail me at someone@elsewhere.test"
    call = ToolCall(
        id="c", name="get_artifact", arguments={"artifact_id": f"token={FAKE_CREDENTIALS[2]}"}
    )
    await model.complete(
        [
            Message("user", RedactedText(leaked)),
            Message("assistant", text(""), tool_calls=(call,)),
            Message("tool", text("ok"), tool_call_id="c"),
        ]
    )
    sent = json.dumps(provider.requests[0])
    for secret in (*FAKE_CREDENTIALS, "someone@elsewhere.test"):
        assert secret not in sent
    assert "[REDACTED:credential]" in sent
    assert "[REDACTED:email]" in sent


def test_model_names_are_required_settings() -> None:
    settings = Settings(_env_file=None, database_url=DATABASE_URL)
    assert settings.model_interviewer is None
    assert settings.model_drafter is None
    with pytest.raises(ConfigError, match="EKI_MODEL_INTERVIEWER"):
        model_name(settings, "interviewer")
    with pytest.raises(ConfigError, match="EKI_MODEL_DRAFTER"):
        model_name(settings, "drafter")
    configured = Settings(
        _env_file=None,
        database_url=DATABASE_URL,
        model_interviewer="provider/a",
        model_drafter="provider/b",
    )
    assert model_name(configured, "interviewer") == "provider/a"
    assert model_name(configured, "drafter") == "provider/b"
    assert configured.model_timeout_seconds == 60.0


# Real provider IDs look like credentials to the redactor; they must survive unchanged.
PROVIDER_IDS = (
    "toolu_01A09q90qw90lq917835lq9",
    "toolu_01XFDUDYJgAACzvnptvVoYEL",
    "call_9pw1qnYScqvGrCH58HWCvFH6",
)


def test_safe_identifier_keeps_plain_identifiers_and_hashes_the_rest() -> None:
    from eki.llm import safe_identifier

    for value in (*PROVIDER_IDS, "call-1", "record_note"):
        assert safe_identifier(value) == value
    leak = f"password={FAKE_CREDENTIALS[1]}"
    replaced = safe_identifier(leak)
    assert FAKE_CREDENTIALS[1] not in replaced
    assert replaced == safe_identifier(leak), "stable, so a call and its result still match"
    assert replaced != safe_identifier(leak + "x")
    for unsafe in (leak, "", "a" * 129, "has space", "ünïcode"):
        assert re.fullmatch(r"[A-Za-z0-9_-]{1,128}", safe_identifier(unsafe))


async def test_provider_tool_call_ids_survive_the_round_trip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = FakeProvider()
    model = litellm_model(monkeypatch, provider)
    calls = tuple(
        ToolCall(id=i, name="record_note", arguments={"section": "steps"}) for i in PROVIDER_IDS
    )
    await model.complete(
        [
            Message("user", text("Start.")),
            Message("assistant", text(""), tool_calls=calls),
            *(Message("tool", text("Noted."), tool_call_id=i) for i in PROVIDER_IDS),
        ]
    )
    sent = provider.requests[0]["messages"]
    assert [c["id"] for c in sent[1]["tool_calls"]] == list(PROVIDER_IDS)
    assert [m["tool_call_id"] for m in sent[2:]] == list(PROVIDER_IDS)
    assert "[REDACTED" not in json.dumps(sent)


async def test_traces_keep_provider_tool_call_ids() -> None:
    from eki.llm.tracing import TracedModel
    from tests.fakes import RecordingTracer

    tracer = RecordingTracer()
    call = ToolCall(PROVIDER_IDS[0], "ask_expert", {"question": "Why?"})
    inner = ScriptedModel([Reply(text="", tool_calls=(call,))])
    model = TracedModel(inner, tracer, step="interview", work_email_domains=WORK_DOMAINS)
    await model.complete([Message("user", text("Start."))])
    assert tracer.calls[0].tool_calls[0].id == PROVIDER_IDS[0]
