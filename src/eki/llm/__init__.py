"""The model interface.

Every model call in the project goes through ``ChatModel``. Message content is
``RedactedText``: a model may only be sent text that has been through
``eki.ingest.redaction.redact``. Tests use a scripted fake behind the same
interface, so no test needs a network or a key.
"""

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from eki.ingest.redaction import RedactedText

Role = Literal["system", "user", "assistant", "tool"]


_IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,128}")


def safe_identifier(value: str) -> str:
    """A tool-call ID or tool name that is safe to send back to a provider and to trace.

    Provider IDs such as ``toolu_01A09q90qw90lq917835lq9`` look like credentials to the
    redactor, and redacting them would give every call the same ID. They are
    identifiers, not content, and the model only ever sees redacted text, so a plain
    identifier is kept as it is. Anything else is replaced by a stable hash, which keeps
    a call and its result matched while dropping whatever the value contained.
    """
    if _IDENTIFIER.fullmatch(value):
        return value
    return "id_" + hashlib.blake2b(value.encode(), digest_size=12).hexdigest()


class ModelError(Exception):
    """The model could not be reached or returned something unusable. Safe to retry."""


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class Message:
    role: Role
    content: RedactedText
    tool_calls: tuple[ToolCall, ...] = ()
    """Only on assistant messages."""
    tool_call_id: str | None = None
    """Only on tool messages: the call this is the result of."""


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    """A JSON Schema object."""


@dataclass(frozen=True)
class Reply:
    text: str
    """Model output. Not yet redacted: redact it before storing or showing it."""
    tool_calls: tuple[ToolCall, ...] = ()
    input_tokens: int = 0
    output_tokens: int = 0


class ChatModel(Protocol):
    @property
    def name(self) -> str: ...

    async def complete(
        self, messages: Sequence[Message], tools: Sequence[ToolSpec] = ()
    ) -> Reply: ...


def message_to_dict(message: Message) -> dict[str, Any]:
    """A plain JSON form, for workflow state."""
    return {
        "role": message.role,
        "content": message.content,
        "tool_calls": [
            {"id": c.id, "name": c.name, "arguments": c.arguments} for c in message.tool_calls
        ],
        "tool_call_id": message.tool_call_id,
    }


def message_from_dict(data: dict[str, Any]) -> Message:
    return Message(
        role=data["role"],
        # Workflow state only ever holds messages that were built from redacted text.
        content=RedactedText(data["content"]),
        tool_calls=tuple(
            ToolCall(id=c["id"], name=c["name"], arguments=dict(c["arguments"]))
            for c in data.get("tool_calls", [])
        ),
        tool_call_id=data.get("tool_call_id"),
    )
