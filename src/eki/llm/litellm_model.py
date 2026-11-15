"""``ChatModel`` backed by LiteLLM, so the provider is a matter of configuration.

LiteLLM reads the provider key from the provider's usual environment variable
(for example ``GROQ_API_KEY``); this module never sees it.
"""

import json
import os
from collections.abc import Sequence
from typing import Any

from eki.ingest.redaction import redact
from eki.llm import Message, ModelError, Reply, ToolCall, ToolSpec, safe_identifier


class LiteLLMModel:
    def __init__(
        self,
        name: str,
        *,
        timeout: float,
        work_email_domains: frozenset[str],
        max_tokens: int = 4096,
    ) -> None:
        self.name = name
        self._timeout = timeout
        self._domains = work_email_domains
        self._max_tokens = max_tokens

    async def complete(self, messages: Sequence[Message], tools: Sequence[ToolSpec] = ()) -> Reply:
        litellm = _litellm()

        request: dict[str, Any] = {
            "model": self.name,
            "messages": [self._wire(message) for message in messages],
            "timeout": self._timeout,
            "max_tokens": self._max_tokens,
        }
        if tools:
            request["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.parameters,
                    },
                }
                for tool in tools
            ]
        try:
            response = await litellm.acompletion(**request)
        except Exception as error:
            # The provider's own message can echo the prompt, so only its type is kept here.
            raise ModelError(f"{type(error).__name__} from model {self.name}") from None
        return self._reply(response)

    def _clean(self, value: Any) -> Any:
        """Last line of defence: whatever the caller built, nothing unredacted leaves here."""
        if isinstance(value, str):
            return str(redact(value, work_email_domains=self._domains).text)
        if isinstance(value, dict):
            return {self._clean(key): self._clean(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._clean(item) for item in value]
        return value

    def _wire(self, message: Message) -> dict[str, Any]:
        content = self._clean(message.content)
        if message.role == "tool":
            return {
                "role": "tool",
                "tool_call_id": safe_identifier(message.tool_call_id or ""),
                "content": content,
            }
        wire: dict[str, Any] = {"role": message.role, "content": content}
        if message.tool_calls:
            wire["tool_calls"] = [
                {
                    "id": safe_identifier(call.id),
                    "type": "function",
                    "function": {
                        "name": safe_identifier(call.name),
                        "arguments": json.dumps(self._clean(call.arguments), ensure_ascii=False),
                    },
                }
                for call in message.tool_calls
            ]
        return wire

    def _reply(self, response: Any) -> Reply:
        try:
            message = response.choices[0].message
        except (AttributeError, IndexError, TypeError):
            raise ModelError(f"empty response from model {self.name}") from None
        calls = tuple(
            ToolCall(id=str(raw.id), name=str(raw.function.name), arguments=_arguments(raw))
            for raw in (message.tool_calls or [])
        )
        usage = getattr(response, "usage", None)
        return Reply(
            text=message.content or "",
            tool_calls=calls,
            input_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
            output_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
        )


def _litellm() -> Any:
    """Import LiteLLM on first use: the import is slow and most commands never call a model.

    By default LiteLLM downloads a price list from the internet when imported; the copy that
    ships with the package is used instead. In its default mode LiteLLM also loads the local
    ``.env`` file, which is how a provider key placed there reaches it.
    """
    os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    import litellm

    return litellm


def _arguments(raw: Any) -> dict[str, Any]:
    """Tool arguments as a dict. Malformed JSON becomes ``{}``; the tool reports what is missing."""
    arguments = raw.function.arguments
    if isinstance(arguments, dict):
        return arguments
    try:
        parsed = json.loads(arguments or "{}")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}
