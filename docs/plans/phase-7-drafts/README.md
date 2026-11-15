# Phase 7 evaluation drafts

These are proposed inputs for the owner to review. They have not been adopted
into the human-owned `evals/scenarios/` and `evals/golden/` directories. No runtime
code reads the fixture answer key or expert answers: the synthetic scripts were
copied into these standalone files when preparing the drafts.

Validate them without credentials, models or a database:

```bash
uv run python evals/grade.py --dry-run \
  --scenarios docs/plans/phase-7-drafts/scenarios \
  --golden docs/plans/phase-7-drafts/golden
```

Each G1–G8 scenario identifies its topic by planted source artifact IDs. G6's
wiki page is deliberately excluded from `topic_artifact_ids`: documents inform
gap scoring but are not topic members. The runner chooses the smallest topic
holding at least 80% of the listed IDs. It reads the synthetic Git, tickets and
selected `Inbox`, `Sent` and `Vendors` mail folders through the read-only MCP
servers. Personal/private mail exclusions still apply.

Goldens require facts across all seven sections for G1–G6 and G8. G3 withholds
rollback initially and requires a rollback follow-up. G1 requires both
contradicting restart-order ticket IDs in one question. G7 answers only “I don't
remember”; every section must remain open with no notes. All eight pass with
the scripted checklist interviewer. G1's contradiction question is supplied by the existing planner;
the double asks that drafted question first. This validates the integration
and file expectations without measuring real-model judgement.

Phrase matching ignores case but does not recognise semantic paraphrases.
Grounding compares note words and numbers with the topic's non-confidential
expert turns. Review the selected phrases, maximum ten questions and grounding
allowance of two unsupported terms per note before treating scores as a quality
gate. One run is one sample.

After review, the owner can copy or move matching pairs into the human-owned
directories. Agents and automation must not perform that adoption. To run these
drafts directly with explicitly opted-in models, use the same directory flags
without `--dry-run`; see [the eval guide](../../../evals/README.md).
