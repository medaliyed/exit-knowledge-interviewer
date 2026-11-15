"""A simulated expert that answers one topic from a scenario script.

The first question gets everything the expert volunteers, one labelled line per
section. A later question about a section gets the follow-up for it, or the
initial answer again. A freeform script says only its one line, whatever is
asked. The same double drives the synthetic acceptance tests.
"""

from typing import Any

from eki.evals.models import Script
from eki.interview.models import SECTIONS


class SimulatedExpert:
    def __init__(self, script: Script | dict[str, Any] | None) -> None:
        self._script = Script.model_validate(script) if isinstance(script, dict) else script
        self._asked = False

    def answer(self, section: str | None) -> str:
        if self._script is None:
            return "I don't remember"
        if self._script.freeform:
            return self._script.freeform
        initial, follow_ups = self._script.initial, self._script.follow_ups
        if not self._asked:
            self._asked = True
            return "\n".join(
                f"{name.capitalize()}: {initial[name]}" for name in SECTIONS if initial[name]
            )
        if section in SECTIONS:
            return follow_ups.get(section) or initial.get(section) or "I don't know."
        return "I don't know."
