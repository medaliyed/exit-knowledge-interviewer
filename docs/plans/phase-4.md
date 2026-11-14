# Phase 4: Harden (F3, F13)

Status: authorized by the user's instruction to implement Phase 4; work in progress.
No new dependencies, no new agentic workflow node, no real source systems and no
changes to human-owned `evals/scenarios/` or `evals/golden/`.

## Decisions

- Audit is durable Postgres metadata, independent of best-effort Langfuse traces.
  Events have a stable user subject, run ID, correlation ID, call ID, phase and
  outcome. No prompts, answers, queries, credentials, reasons, mail bodies or tool
  arguments/results are stored in audit metadata. Fixed/validated operation names
  and counts only. Multi-run retrieval calls emit one scoped event per affected
  run, linked by call ID, without disclosing other run IDs to an owner.
- Model/provider and tool attempts are recorded before execution; audit failure
  refuses the call. Completion/failure records distinguish attempts whose process
  stopped before completion. Approvals, review and ACL changes are audited in the
  same transaction as their state changes. Append-only SQL triggers reject normal
  update/delete/truncate; database administrators remain trusted. Run deletion
  does not delete audit history. API audit reads require owner/admin; local CLI
  audit reads retain the existing trusted-operator boundary.
- API context propagates the authenticated subject through async work/threads;
  local CLI work uses `local-operator`. Correlation IDs are generated internally,
  never trusted from request headers. Direct service tests use the local identity.
  All runtime chat/provider calls, interviewer tools, and ingestion MCP calls are
  instrumented. Audit history is paginated with stable event IDs.
- Expand automated adversarial tests for prompt/tool injection, confidential and
  secret text, metadata leakage, forged citations, path/symlink scope escapes,
  access revocation, failed audit storage, and read-only connectors.
- Quality evaluations live in `tests/` and a reproducible test runner outside the
  human-owned scenario/golden directories. Add explicit `eval` and `redteam`
  pytest markers and a CI quality gate with machine-readable reports. Thresholds
  and expected behavior are fixed, independently asserted, and not derived from
  the implementation's scores. Existing fixture keys remain test-only inputs.
- Add opt-in live semantic retrieval and evidence-selector checks. Tests require
  explicitly configured models/provider credentials and skip with clear reasons
  otherwise. No provider or model is silently selected, no live costs in default
  tests or CI. Run available validation and report missing credentials honestly.
- Mail is read-only synthetic JSON through a dedicated MCP server, consistent
  with existing source adapters. Explicit `--mail-folder` selections fix scope
  before server startup; default is no mail. Block personal/private folder names
  and sensitivity flags regardless of selection. Restrict file access to selected
  folder descendants, reject traversal and symlink escapes, and revalidate every
  returned message at the client boundary. Exclude attachments/HTML expansion,
  remote mailbox authentication, sending, editing and deleting mail.
- Mail artifacts use source `mail`, kind `email`, and thread grouping for topic
  mapping. Only expert-involved messages within the selected folders/date range
  enter the pipeline; other messages' content is never returned. Redact body,
  subject, participants and metadata before returning/storing/indexing. Preserve
  original IDs for artifact citations. Unknown/private sensitivity is excluded.

## Deliverables (test first, one commit per task)

- [x] a / F13: append-only audit schema/store, contextual model and tool wrappers,
  atomic review/approval/ACL events, API/CLI read access and correlation tests.
- [ ] b / P4-b: adversarial and quality evaluation suites with explicit measurable
  assertions; add deterministic CI gates and JUnit reports without live keys.
- [ ] c / P4-c: opt-in live search/evidence-selection validation and an honest
  run report; skip unavailable provider configurations without inventing results.
- [ ] d / F3: selected-folder read-only mail MCP adapter, client revalidation,
  ingestion/CLI integration, thread topics, privacy and corpus acceptance tests.
- [ ] e / P4-e: full end-to-end acceptance, documentation, results and limits.

After each deliverable run and report `uv run pytest -q`,
`uv run pytest -q -m db`, `uv run ruff check .`,
`uv run ruff format --check .`, `uv run mypy`; commit only verified work.
Finish with the infra checks and explicit evaluation suites. Never put real
credentials, private sources or confidential text in tests/reports/logs.
