"""F9: a claim that says more than the answer it cites is pointed out to the reviewer."""

from typing import Any

import pytest

from eki.ingest.redaction import RedactedText
from eki.interview.models import Turn
from eki.runbook.faithfulness import Unsupported, check_entry, unsupported_terms
from eki.runbook.models import RunbookEntry

SESSION = "5f0c1c5e-0000-4000-8000-000000000001"
TOPIC = "incident:billing:stale"
ANSWER = "I restarted the ledger-db replicas and then started billing-batch again."


@pytest.mark.parametrize(
    ("claim", "answer", "missing"),
    [
        ("Restart the ledger-db replicas, then start billing-batch again.", ANSWER, ()),
        ("Restarting replicas; starts the batch.", ANSWER, ()),
        ("RESTART THE REPLICAS", ANSWER, ()),
        ("Restart the replicas after 15 minutes.", ANSWER, ("15", "minutes")),
        ("Restart the replicas and notify finance.", ANSWER, ("notify", "finance")),
        ("Wait until 02:00.", "Wait until 2 at night.", ("02", "00")),
        ("Do not restart the replicas.", ANSWER, ("not",)),
        ("Don't restart the replicas.", ANSWER, ("not",)),
        ("Never restart the replicas.", ANSWER, ("never",)),
        ("Do not restart it.", "You must not restart it.", ()),
        ("Apply the fix; it applies to toggled flags.", "I applied a fix and toggle the flag.", ()),
        ("Use [REDACTED:credential] to log in.", "Log in with [REDACTED:credential].", ("use",)),
        ("Notify finance. Notify finance again.", "Tell them.", ("notify", "finance", "again")),
    ],
)
def test_unsupported_terms(claim: str, answer: str, missing: tuple[str, ...]) -> None:
    assert unsupported_terms(claim, [answer]) == missing


def test_every_source_counts() -> None:
    assert unsupported_terms("Restart twice at 02:00.", ["Restart at 02:00?", "Yes, twice."]) == ()
    assert unsupported_terms("Restart twice.", []) == ("restart", "twice")


def turn(ordinal: int, text: str, **fields: Any) -> Turn:
    return Turn.model_validate(
        {
            "ordinal": ordinal,
            "topic_id": TOPIC,
            "speaker": "expert",
            "kind": "answer",
            "text": RedactedText(text),
            **fields,
        }
    )


def question(ordinal: int, text: str) -> Turn:
    return turn(ordinal, text, speaker="interviewer", kind="question")


def claim(text: str, *turns: int) -> dict[str, Any]:
    return {
        "text": text,
        "citations": [
            {"kind": "interview_turn", "session_id": SESSION, "turn_index": n} for n in turns
        ],
    }


def entry(**sections: Any) -> RunbookEntry:
    return RunbookEntry.model_validate(
        {"id": "entry-1", "topic_id": TOPIC, "title": "Stale batch", **sections}
    )


def test_each_claim_is_checked_against_the_turn_it_cites() -> None:
    turns = [
        question(1, "What do you do when the batch is stale?"),
        turn(2, "Restart the replicas.", answers=1),
        question(3, "Is the rollback to restore the snapshot from 02:00?"),
        turn(4, "Yes. Ask finance first.", answers=3),
    ]
    checked = check_entry(
        entry(
            steps={
                "claims": [
                    claim("Restart the replicas.", 2),
                    claim("Ask finance first.", 2),
                    claim("Restart the replicas and ask finance.", 2, 4),
                ]
            },
            rollback={"claims": [claim("Restore the snapshot from 02:00, ask finance first.", 4)]},
            pitfalls={"claims": [claim("Finance closes the books at 18:00.", 4)]},
        ),
        turns,
    )
    assert checked == (
        Unsupported("steps", 1, ("ask", "finance", "first")),
        Unsupported("pitfalls", 0, ("closes", "books", "18")),
    ), "another turn does not support a claim; a confirmed question and a second citation do"
    assert str(checked[0]) == "steps[1]: not in the cited answer: ask, finance, first"


def test_only_what_the_claim_cites_is_read() -> None:
    turns = [
        question(1, "Who do you call?"),
        turn(2, "The private number is 555 0100.", answers=1, confidential=True),
        turn(3, "Call the duty phone.", answers=1),
    ]
    artifact = {"text": "Rotate the key.", "citations": [{"kind": "artifact", "artifact_id": "T1"}]}
    checked = check_entry(
        entry(
            contacts={
                "claims": [
                    claim("Call 555 0100.", 2),
                    claim("Call the duty desk.", 9),
                    claim("Call 555 0100 or the duty phone.", 2, 3),
                ]
            },
            steps={"claims": [artifact, claim("Call the duty phone.", 3)]},
        ),
        turns,
    )
    assert checked == (), (
        "a claim with a confidential, unknown or artifact source is not compared at all"
    )
