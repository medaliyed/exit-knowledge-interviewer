"""Test doubles for the model: nothing here talks to a network."""

import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from eki.evals.expert import SimulatedExpert as SimulatedExpert
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


_LABELLED = re.compile(
    r"^(Trigger|Diagnosis|Steps|Rollback|Contacts|Pitfalls|Rationale): (.+)$", re.MULTILINE
)
_UNKNOWN = re.compile(r"don't (remember|know)", re.IGNORECASE)
_PROGRESS = re.compile(
    r"\n\nInterview status \(not part of the answer\): "
    r"(Still missing: [a-z, ]+\.|Every section is covered\.)$"
)
_FIRST_DRAFTED = re.compile(r"^1\. (.+)$", re.MULTILINE)


def checklist_interviewer(messages: Sequence[Message], tools: Sequence[ToolSpec]) -> Reply:
    """An interviewer policy that needs no model: ask, note what was answered, ask what is missing.

    It follows the same rules the real interviewer is prompted with, so a whole interview can
    run in a test. It proves the workflow, not the judgement of a real model.
    """
    handled: set[str] = set()
    asked: dict[str, str] = {}
    for message in messages:
        for call in message.tool_calls:
            if call.name in ("record_note", "mark_open"):
                handled.add(str(call.arguments["section"]))
            elif call.name == "ask_expert":
                asked[call.id] = str(call.arguments["section"])
        resolved = "skipped this question" in message.content or "confidentially" in message.content
        if message.role == "tool" and message.tool_call_id in asked and resolved:
            handled.add(asked[message.tool_call_id])
    calls: list[tuple[str, dict[str, Any]]] = []
    last = messages[-1]
    answered = last.role == "tool" and last.tool_call_id in asked
    if answered and "The expert answered:\n" in last.content:
        section = asked[str(last.tool_call_id)]
        answer = _PROGRESS.sub("", last.content.split("The expert answered:\n", 1)[1])
        labelled = _LABELLED.findall(answer)
        if labelled:
            found = [(label.lower(), text) for label, text in labelled]
            calls += [
                ("record_note", {"section": name, "text": text})
                for name, text in found
                if name not in handled
            ]
            handled |= {name for name, _ in found}
        elif _UNKNOWN.search(answer):
            targets = (
                [section] if len(asked) > 1 else [s for s in SECTION_ORDER if s not in handled]
            )
            reason = f'The expert said: "{answer}"'
            calls += [("mark_open", {"section": name, "reason": reason}) for name in targets]
            handled |= set(targets)
        else:
            calls.append(("record_note", {"section": section, "text": answer}))
            handled.add(section)
    missing = [name for name in SECTION_ORDER if name not in handled]
    if not missing:
        calls.append(("finish_topic", {}))
    elif not asked:
        drafted = _FIRST_DRAFTED.search(messages[1].content)
        question = drafted.group(1) if drafted else "What should your successor know about this?"
        calls.append(("ask_expert", {"question": question, "section": missing[0]}))
    else:
        question = f"You did not mention the {missing[0]}. What is it?"
        calls.append(("ask_expert", {"question": question, "section": missing[0]}))
    return tool_reply(*calls)
