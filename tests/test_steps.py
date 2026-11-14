"""F9: a procedure is cut into steps where the expert ended one, and nowhere else."""

import pytest

from eki.runbook.steps import split_steps


@pytest.mark.parametrize(
    ("said", "steps"),
    [
        # Plain sentences, including the sloppy lower-case start of typed chat.
        ("First. Second!", ["First.", "Second!"]),
        ("Restart it. then verify? Done", ["Restart it.", "then verify?", "Done"]),
        ("  One step only  ", ["One step only"]),
        # Abbreviations that never end a step.
        (
            "Check the logs, e.g. ledger.log. Restart it.",
            ["Check the logs, e.g. ledger.log.", "Restart it."],
        ),
        ("Use the replica, i.e. Ledger-2. Retry.", ["Use the replica, i.e. Ledger-2.", "Retry."]),
        ("Ask Dr. Brandl. Then restart.", ["Ask Dr. Brandl.", "Then restart."]),
        ("Ask T. Brandl or Mrs. Okafor first.", ["Ask T. Brandl or Mrs. Okafor first."]),
        ("Compare old vs. new totals. Report.", ["Compare old vs. new totals.", "Report."]),
        ("Open ticket No. 12. Close it.", ["Open ticket No. 12.", "Close it."]),
        # Abbreviations that may end a step: they do so only before a capital letter.
        ("Wait approx. 5 min. Retry.", ["Wait approx. 5 min.", "Retry."]),
        ("Wait 5 min. per replica. Retry.", ["Wait 5 min. per replica.", "Retry."]),
        (
            "Restart workers, replicas etc. Then verify.",
            ["Restart workers, replicas etc.", "Then verify."],
        ),
        ("Restart workers etc. in that order.", ["Restart workers etc. in that order."]),
        (
            "Run it at 2 a.m. Check at 6 a.m. the next day.",
            ["Run it at 2 a.m.", "Check at 6 a.m. the next day."],
        ),
        # Things that look like a full stop but are not one.
        ("Use v2.3 of the job. Done.", ["Use v2.3 of the job.", "Done."]),
        ("Edit deploy/cron.yaml. Reload.", ["Edit deploy/cron.yaml.", "Reload."]),
        ("Is it safe? No. Stop there.", ["Is it safe?", "No.", "Stop there."]),
        ("Wait... Then retry.", ["Wait...", "Then retry."]),
    ],
)
def test_sentences_become_steps(said: str, steps: list[str]) -> None:
    assert list(split_steps(said)) == steps


@pytest.mark.parametrize(
    ("said", "steps"),
    [
        # The expert numbered the steps: the numbers decide, and are not kept twice.
        (
            "1. Open the profile, e.g. the EUR one. Save. 2. Re-run the batch. 3) Confirm.",
            ["Open the profile, e.g. the EUR one. Save.", "Re-run the batch.", "Confirm."],
        ),
        (
            "1. Stop the worker.\n2. Clear the queue. Check it is empty.\n3. Start the worker.",
            ["Stop the worker.", "Clear the queue. Check it is empty.", "Start the worker."],
        ),
        ("- Stop the worker\n- Start the worker\n", ["Stop the worker", "Start the worker"]),
        ("* Stop it.\n\n• Start it.", ["Stop it.", "Start it."]),
        # Lines without markers are boundaries, and are still cut into sentences.
        (
            "Stop the worker. Wait.\nStart the worker.",
            ["Stop the worker.", "Wait.", "Start the worker."],
        ),
    ],
)
def test_an_explicit_list_is_respected(said: str, steps: list[str]) -> None:
    assert list(split_steps(said)) == steps


@pytest.mark.parametrize(
    ("said", "steps"),
    [
        # A number that ends a sentence is not the start of a list.
        (
            "Set retries to 1. Then set the timeout to 2. Restart.",
            ["Set retries to 1.", "Then set the timeout to 2.", "Restart."],
        ),
        ("1. Set workers to 2. Restart.", ["1. Set workers to 2.", "Restart."]),
        ("1. Set workers to 2. 2. Restart.", ["Set workers to 2.", "Restart."]),
        ("1. Set workers to 3. Set retries to 2. Restart.", None),
        # A lone marker is never a step of its own.
        ("2. is not where a list starts. Really.", ["2. is not where a list starts.", "Really."]),
        ("1. A single numbered line is one step.", ["1. A single numbered line is one step."]),
        ("- a single bulleted line", ["- a single bulleted line"]),
    ],
)
def test_numbers_inside_sentences_are_not_list_markers(said: str, steps: list[str] | None) -> None:
    found = list(split_steps(said))
    assert " ".join(found) == said or steps is not None, "nothing is removed"
    if steps is not None:
        assert found == steps


def test_nothing_is_reworded_or_lost() -> None:
    said = "Check the logs, e.g. ledger.log. Ask Dr. Brandl at 2 a.m. if needed. Restart it."
    assert " ".join(split_steps(said)) == said
    assert split_steps("") == () and split_steps(" \n ") == ()
