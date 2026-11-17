# F9/F10: batch and reconcile entry reads

Authorized by the user on 2026-11-17: tackle the entry-read consolidation review item.

Provide service operations for batched entry summaries and a complete owner detail.
Use one borrowed connection and a read-only repeatable-read transaction per operation
so content, versions, faithfulness, maintenance metadata and decision history share
a database snapshot. Batch metadata and explicit follow-up lineage for summaries,
load each required transcript once, and check only the requested entry for detail.
Keep reader detail on its approved-only, access-filtered path and use the same
snapshot discipline without exposing private review data.

Keep API response fields, ordering, authorization, CLI behavior and mutations
compatible. No dependencies or migrations. Add regressions for query growth,
concurrent changes, missing/empty resources, linked-source isolation and reader
privacy. Run affected database/API tests, the Python suite, Ruff and mypy; update
the review flow and commit the verified result.

## Outcome

Implemented typed summary/detail read results in `runbook/reads.py`, exposed
through `RunbookService.summaries` and `detail`. The HTTP listing, synthesis and
mutation responses use these operations. Metadata and explicit follow-up lineage
are batched, entries are loaded once for summaries, and each required transcript
is loaded once per snapshot. Reader detail retains approved-only access filtering
and shares a snapshot with its metadata. No API fields, dependencies or schemas
changed.

Validation on 2026-11-17:

- All 916 deterministic Python tests passed, including database tests and the
  real browser/API acceptance journey; 11 live/infra tests were deselected.
- Six new regressions cover constant query cost for one versus several entries,
  one service connection checkout, concurrent summary/detail edits, reader
  consistency, explicit follow-up source isolation and missing/empty resources.
- The reader regression reproduced mixed old content and new metadata before
  the snapshot change. All new tests now pass.
- Ruff lint/format, mypy and whitespace checks passed. The tests used synthetic
  data and scripted models in a disposable Postgres/pgvector database.
