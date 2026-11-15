"""A model wrapper that stops a runaway interview before it burns the budget."""

from collections.abc import Sequence
from dataclasses import dataclass

from eki.llm import ChatModel, Message, ModelError, Reply, ToolSpec


@dataclass
class CallBudget:
    """One allowance shared by the drafter and interviewer of a scenario."""

    max_calls: int
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    stopped: bool = False

    def __post_init__(self) -> None:
        if self.max_calls < 1:
            raise ValueError("max_calls must be at least 1")


class BudgetedModel:
    """Passes ``max_calls`` calls through, then fails every call without reaching the model."""

    def __init__(
        self, inner: ChatModel, *, max_calls: int = 40, budget: CallBudget | None = None
    ) -> None:
        self._inner = inner
        self.budget = budget if budget is not None else CallBudget(max_calls)

    @property
    def calls(self) -> int:
        return self.budget.calls

    @property
    def name(self) -> str:
        return self._inner.name

    async def complete(self, messages: Sequence[Message], tools: Sequence[ToolSpec] = ()) -> Reply:
        if self.budget.calls >= self.budget.max_calls:
            self.budget.stopped = True
            raise ModelError(f"model-call budget of {self.budget.max_calls} calls spent")
        self.budget.calls += 1
        reply = await self._inner.complete(messages, tools)
        self.budget.input_tokens += reply.input_tokens
        self.budget.output_tokens += reply.output_tokens
        return reply
