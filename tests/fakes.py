"""Test doubles for the model: nothing here talks to a network."""

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from eki.llm import Message, Reply, ToolSpec

Policy = Callable[[Sequence[Message], Sequence[ToolSpec]], Reply]


@dataclass(frozen=True)
class Call:
    messages: tuple[Message, ...]
    tools: tuple[ToolSpec, ...]


class ScriptedModel:
    """Replays fixed replies in order, or asks a policy function, and records what it was sent."""

    def __init__(
        self, script: Sequence[Reply | Exception] | Policy, name: str = "scripted-model"
    ) -> None:
        self.name = name
        self.calls: list[Call] = []
        self._policy: Policy | None = None
        self._replies: list[Reply | Exception] = []
        if callable(script):
            self._policy = script
        else:
            self._replies = list(script)

    async def complete(self, messages: Sequence[Message], tools: Sequence[ToolSpec] = ()) -> Reply:
        self.calls.append(Call(tuple(messages), tuple(tools)))
        if self._policy is not None:
            return self._policy(messages, tools)
        if not self._replies:
            raise AssertionError(f"{self.name}: the script has no reply left")
        item = self._replies.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    @property
    def remaining(self) -> int:
        return len(self._replies)

    def sent_text(self) -> str:
        """Everything this model was ever sent, as one string, for scanning."""
        parts: list[str] = []
        for call in self.calls:
            for message in call.messages:
                parts.append(message.content)
                parts += [json.dumps(c.arguments, ensure_ascii=False) for c in message.tool_calls]
            parts += [json.dumps(t.parameters) + t.description for t in call.tools]
        return "\n".join(parts)
