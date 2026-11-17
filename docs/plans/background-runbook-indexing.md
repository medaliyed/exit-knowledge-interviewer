# F11/F12: durable runbook indexing outside search requests

Authorized by the user on 2026-11-17 as review point 3.

Queue approved entry versions transactionally in Postgres for registered embedding
models. API lifespan and a standalone CLI worker register the configured model,
backfill existing approvals and process retryable jobs. Session advisory locks
deduplicate active work across processes and release on process death. Provider
calls use a bounded worker's dedicated connection, never a request-pool connection.
Recheck approval and version under run/entry locks before atomic publication;
drafts, corrections and withdrawals invalidate indexes and obsolete older jobs.
Metadata-only review changes preserve embeddings. Failures retain sanitized job
state with backoff; worker restart reclaims abandoned processing jobs.

Search only reads prepared indexes. Its query embedding happens between short
connection borrows. Expose approved-only ACL-filtered readiness in the browser;
pending indexing must not look like a confirmed unsupported Ask result. Preserve
model-space separation, redaction, audit and live publication/access checks.

Test queueing/backfill, no search-side indexing, concurrent workers, retries,
crash recovery, stale publication, connection use and privacy. Update affected
fixtures to run indexing explicitly, verify provider-free suites and browser
behavior, rebuild assets, document migration/worker operation and commit.

## Outcome

Implemented migration 0019 with transactional approval/change triggers, registered
model identities and durable job state. The API owns one bounded worker per
process; `eki index-runbook` supports standalone operation and bounded one-shot
batches. Workers use session advisory locks across provider work, reclaim abandoned
processing jobs and publish only complete still-approved versions. Provider and
audit failures retain fixed codes and back off from 5 to 300 seconds. Registration
backfills existing approvals and reconciles every 30 seconds, including approvals
racing first registration. Existing prepared indexes remain usable on rollout.

Search no longer indexes entry content and releases its request-pool connection
before query embedding. The approved-only ACL-filtered readiness endpoint, search
JSON metadata and browser preparation/retry labels distinguish pending knowledge
from unsupported questions. Ask's new `indexing` reason prevents premature
follow-up requests. Approval, corrections, maintenance and cross-session citation
rules remain enforced. API worker construction honors injected settings.

Validation on 2026-11-17:

- 910 provider-free Python tests passed; 11 live/infra tests deselected. This
  includes the real-browser/API acceptance journey with the rebuilt assets.
- All 60 Playwright browser tests and 11 frontend unit tests passed; production
  build, Ruff lint/format, mypy, TypeScript, Prettier and whitespace checks passed.
- New database regressions cover approval/job rollback, model backfill, migration
  of existing valid/missing indexes, absence of search-side indexing, provider and
  audit failures, retry backoff, concurrent workers, process death, stale writes,
  request-pool availability, standalone CLI processing and automatic API work.
- No live provider or source was used. Validation used disposable Postgres/pgvector
  schemas; the disposable container was removed afterward.

Rollout: run `uv run eki migrate`, then restart the API. For CLI-only use, run
`uv run eki index-runbook --once` before searching new approvals, or keep the
standalone worker running for automatic retries. A crash before index publication
can repeat an external embedding call; concurrent claims are deduplicated.
