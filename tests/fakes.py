"""Test doubles for the model: nothing here talks to a network."""

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from eki.llm import Message, Reply, ToolCall, ToolSpec
from eki.llm.tracing import ModelCall

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


def tool_reply(*calls: tuple[str, dict[str, Any]], text: str = "") -> Reply:
    """A model reply that calls tools, with stable call IDs."""
    global _call_counter
    made = []
    for name, arguments in calls:
        _call_counter += 1
        made.append(ToolCall(id=f"call-{_call_counter}", name=name, arguments=arguments))
    return Reply(text=text, tool_calls=tuple(made))


_call_counter = 0
SECTION_ORDER = ("trigger", "diagnosis", "steps", "rollback", "contacts", "pitfalls", "rationale")


def expert_answers() -> dict[str, Any]:
    """The simulated expert's scripted answers. Tests only: the pipeline never reads this file."""
    path = Path(__file__).resolve().parent / "fixtures" / "expert_answers.json"
    answers: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))["answers"]
    return answers


class SimulatedExpert:
    """Answers for one topic the way the fixture says the expert would.

    The first question gets everything she volunteers, one labelled line per
    section. A later question about a section gets her follow-up for it.
    """

    def __init__(self, script: dict[str, Any] | None) -> None:
        self._script = script
        self._asked = False

    def answer(self, section: str | None) -> str:
        if self._script is None or self._script["freeform"]:
            return "I don't remember" if self._script is None else str(self._script["freeform"])
        initial, follow_ups = self._script["initial"], self._script["follow_ups"]
        if not self._asked:
            self._asked = True
            return "\n".join(
                f"{name.capitalize()}: {initial[name]}" for name in SECTION_ORDER if initial[name]
            )
        return str(follow_ups.get(section) or initial.get(section) or "I don't know.")


class RecordingTracer:
    """Keeps every traced model call, for assertions."""

    def __init__(self) -> None:
        self.calls: list[ModelCall] = []
        self.flushed = 0

    def record(self, call: ModelCall) -> str | None:
        self.calls.append(call)
        return None

    def flush(self) -> None:
        self.flushed += 1

    def text(self) -> str:
        """Everything that was traced, as one string, for scanning."""
        parts: list[str] = []
        for call in self.calls:
            parts += [m.content for m in call.messages]
            parts += [json.dumps(c.arguments) for m in call.messages for c in m.tool_calls]
            parts += [call.reply_text or "", call.error or ""]
            parts += [json.dumps(c.arguments) for c in call.tool_calls]
        return "\n".join(parts)
