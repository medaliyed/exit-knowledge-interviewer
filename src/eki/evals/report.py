"""Printable scorecards, deterministic report comparisons and JSON persistence."""

from pathlib import Path

from eki.evals.models import Report, ScenarioResult


def render(report: Report) -> str:
    lines: list[str] = []
    for result in report.scenarios:
        state = "PASS" if result.passed else "ABORTED" if result.aborted else "FAIL"
        lines += [f"{result.scenario}: {state}", "check | result | pass", "--- | --- | ---"]
        lines.append(f"finished | {result.finished} | {'yes' if result.finished else 'no'}")
        for check in result.checks:
            lines.append(
                f"{check.name} | {check.found}/{check.expected} | {'yes' if check.passed else 'no'}"
            )
            lines.extend(f"  {detail}" for detail in check.details)
        if result.error:
            lines.append(f"error: {result.error}")
        lines.append(
            f"{result.questions} questions; {result.model_calls} model calls; "
            f"{result.input_tokens}/{result.output_tokens} input/output tokens; "
            f"{result.seconds:.2f}s"
        )
        lines.append("")
    lines.append(f"{report.passed} of {len(report.scenarios)} passed")
    return "\n".join(lines)


def _numbers(result: ScenarioResult) -> dict[str, int]:
    numbers = {
        "questions": result.questions,
        "model_calls": result.model_calls,
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
    }
    for check in result.checks:
        numbers[f"{check.name}.found"] = check.found
        numbers[f"{check.name}.expected"] = check.expected
    return numbers


def compare(previous: Report, current: Report) -> tuple[str, bool]:
    """Return readable changes and whether any previously passing scenario regressed.

    A removed passing scenario counts as a regression: filtering a run must not
    silently hide a previously measured success.
    """
    before = {s.scenario: s for s in previous.scenarios}
    after = {s.scenario: s for s in current.scenarios}
    lines = ["scenario | previous | current | changed", "--- | --- | --- | ---"]
    regressed = False
    for name in sorted(before.keys() | after.keys()):
        old, new = before.get(name), after.get(name)
        old_state = "missing" if old is None else "PASS" if old.passed else "FAIL"
        new_state = "missing" if new is None else "PASS" if new.passed else "FAIL"
        regressed |= old is not None and old.passed and (new is None or not new.passed)
        changes = []
        if old is not None and new is not None:
            a, b = _numbers(old), _numbers(new)
            changes = [
                f"{key}: {a.get(key, 'missing')} -> {b.get(key, 'missing')}"
                for key in sorted(a.keys() | b.keys())
                if a.get(key) != b.get(key)
            ]
        lines.append(f"{name} | {old_state} | {new_state} | {'; '.join(changes) or '-'}")
    lines += [
        f"Previous: {previous.passed} of {len(previous.scenarios)} passed",
        f"Current: {current.passed} of {len(current.scenarios)} passed",
    ]
    return "\n".join(lines), regressed


def save(report: Report, path: Path) -> None:
    """Create a new report without overwriting a previous run."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        stream.write(report.model_dump_json(indent=2) + "\n")
