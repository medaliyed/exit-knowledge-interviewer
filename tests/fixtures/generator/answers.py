"""Scripted answers for the simulated expert, one entry per planted gap.

``initial`` is what she says when first asked, per template section. ``None``
means she did not cover that section. ``follow_ups`` is what she says when the
interviewer asks again about a section. ``freeform`` is a reply that fits no
section. Two entries are deliberately imperfect: G3 gives no rollback until
asked, and G7 is "I don't remember", which must be recorded as open and never
filled in.
"""

from typing import Any

SECTIONS = ("trigger", "diagnosis", "steps", "rollback", "contacts", "pitfalls", "rationale")


def _answer(
    initial: dict[str, str | None],
    follow_ups: dict[str, str] | None = None,
    freeform: str | None = None,
) -> dict[str, Any]:
    return {
        "initial": {section: initial.get(section) for section in SECTIONS},
        "follow_ups": follow_ups or {},
        "freeform": freeform,
    }


def expert_answers(expert_name: str) -> dict[str, Any]:
    answers = {
        "G1": _answer(
            {
                "trigger": "billing-batch aborts in the posting phase with 'ledger snapshot stale'.",
                "diagnosis": "Check the worker log for the stale snapshot line and replica lag.",
                "steps": "Restart the ledger-db replicas, wait for them to catch up, re-run the batch.",
                "rollback": "Nothing to roll back; the failed run posts nothing.",
                "contacts": "Platform on-call. Finance only if the run finishes after 06:00 UTC.",
                "pitfalls": "Re-running without the restart fails the same way and wastes an hour.",
                "rationale": (
                    "The batch reads from the replicas, and after a failed run they keep "
                    "serving the stale snapshot until they are restarted."
                ),
            },
            follow_ups={
                "steps": (
                    "Replicas first, always, then the workers. The ticket note that says "
                    "workers first is wrong."
                )
            },
        ),
        "G2": _answer(
            {
                "trigger": "Finance or the regulator needs an export, or the nightly export fails.",
                "diagnosis": "Look at the export job log and the row counts per sheet.",
                "steps": "Fix forward in the writer, run the export for the period by hand, compare counts.",
                "rollback": "Re-publish the previous period's file from the archive bucket.",
                "contacts": "Finance operations for content, the regulator liaison for deadlines.",
                "pitfalls": "Column order is contractual; never reorder columns.",
                "rationale": "Nobody else picked it up, so the knowledge stayed with me.",
            }
        ),
        "G3": _answer(
            {
                "trigger": "Month-end totals differ between billing-batch and ledger-db.",
                "diagnosis": "Compare the last posting sequence number on both sides.",
                "steps": "Re-run the reconciliation job with the replay flag for the last day of the month.",
                "rollback": None,
                "contacts": "Finance operations, who report it and confirm the totals.",
                "pitfalls": "Run it before the period is closed, or it needs a reopen.",
                "rationale": "Postings made in the last minutes of the month land after the cut-off.",
            },
            follow_ups={
                "rollback": "Restore the reconciliation table from the pre-run export; the job is idempotent."
            },
        ),
        "G4": _answer(
            {
                "trigger": "A gateway certificate renewal is planned.",
                "diagnosis": "Check the expiry date and the vendor's pinned fingerprint list.",
                "steps": "Mail the vendor a week ahead, wait for confirmation, then renew and roll.",
                "rollback": "Re-install the previous certificate while it is still valid.",
                "contacts": "Marek Dolan at the certificate vendor.",
                "pitfalls": "Renewing before the vendor confirms breaks partner traffic.",
                "rationale": "The vendor pins our certificate and has to add the new fingerprint first.",
            }
        ),
        "G5": _answer(
            {
                "trigger": "Someone changes the fiscal period rollover in the posting job.",
                "diagnosis": "Check whether the change touches the period table.",
                "steps": "Keep the special-cased period table; test with the fourth-quarter fixtures.",
                "rollback": "Revert the change before the next quarter close.",
                "contacts": "Finance operations own the fiscal calendar.",
                "pitfalls": "It only fails once a year, at the fourth-quarter close.",
                "rationale": "The fourth quarter has a 53rd week in some years and a plain rollover skips it.",
            }
        ),
        "G6": _answer(
            {
                "trigger": "A ledger-db failover is needed.",
                "diagnosis": "List replication slots on the old primary before promoting.",
                "steps": "Drop the stale replication slot on the old primary, then follow the failover page.",
                "rollback": "Recreate the slot when the old primary rejoins as a replica.",
                "contacts": "Platform on-call and the storage team.",
                "pitfalls": "With the slot left in place the old primary fills its disk with retained log.",
                "rationale": "The slot pins write-ahead log that nothing will ever consume again.",
            }
        ),
        "G7": _answer({}, freeform="I don't remember"),
        "G8": _answer(
            {
                "trigger": "Someone asks why ledger-db is not in the standard backup job.",
                "diagnosis": "Check the separate snapshot job on replica-2.",
                "steps": "Leave it out of the standard job; keep the 05:30 snapshot from replica-2.",
                "rollback": "Add it back only if the settlement window moves.",
                "contacts": "Engineering manager and audit.",
                "pitfalls": "Adding it back to the 02:00 job stalls postings.",
                "rationale": "The standard snapshot lock overlaps the settlement window.",
            }
        ),
    }
    return {"expert": expert_name, "answers": answers}
