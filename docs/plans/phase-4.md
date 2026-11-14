# Phase 4: Harden (F3, F13)

Status: implemented and verified on 2026-11-14, authorized by the user's
instruction to implement Phase 4.
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
- [x] b / P4-b: adversarial and quality evaluation suites with explicit measurable
  assertions; add deterministic CI gates and JUnit reports without live keys.
- [x] c / P4-c: opt-in live search/evidence-selection validation and an honest
  run report; skip unavailable provider configurations without inventing results.
- [x] d / F3: selected-folder read-only mail MCP adapter, client revalidation,
  ingestion/CLI integration, thread topics, privacy and corpus acceptance tests.
- [x] e / P4-e: full end-to-end acceptance, documentation, results and limits.

After each deliverable run and report `uv run pytest -q`,
`uv run pytest -q -m db`, `uv run ruff check .`,
`uv run ruff format --check .`, `uv run mypy`; commit only verified work.
Finish with the infra checks and explicit evaluation suites. Never put real
credentials, private sources or confidential text in tests/reports/logs.


## Live validation run

`uv run pytest -q -m live -rs tests/test_live_retrieval.py`: two skips,
`EKI_MODEL_EMBEDDING` and `EKI_MODEL_ANSWERER` not configured. No provider call
was made. Adapter/guard implementation is verified; actual semantic/provider
quality is explicitly not claimed. Export provider credentials to the test
process and configure the desired models before rerunning these checks.

## Outcome

Implemented in five requirement-scoped deliverables. All verification uses
synthetic sources and disposable database schemas; real source systems and the
human-owned evaluation directories were not changed. No dependencies were added.

| Deliverable | Verified behavior |
| --- | --- |
| F13 | Durable metadata-only audit of model/tool attempts, plan approval, entry review and ACL changes; authenticated actor propagation; owner/admin API and trusted-operator CLI; retained history and append-only SQL guards. |
| P4-b | Adversarial regression checks and a reproducible CI quality gate with JUnit output; redaction of JSON keys/model metadata; safe provider failures and source-path boundaries. |
| P4-c | Explicit opt-in live semantic retrieval and evidence-selector checks, plus guards that skip before provider calls when configuration/credentials are absent. |
| F3 | Explicit selected-folder synthetic mail ingestion, read-only MCP tools, independent client scope validation, saved consent, thread decisions and recurring contact topics. |
| P4-e | Full selected-mail-to-interview-to-approved-runbook flow, confidential-answer isolation, cited Q&A/export, edit demotion and reader revocation. Failed indexing rolls back the snapshot while retaining failure audit; concurrent audit commits cannot be skipped by a cursor. |

Final verification completed successfully:

| Command | Result |
| --- | --- |
| `uv run pytest -q` | 373 passed |
| `uv run pytest -q -m db` | 180 passed |
| `uv run ruff check .` | Passed |
| `uv run ruff format --check .` | Passed |
| `uv run mypy` | Passed |
| `uv run python evals/run.py --junit reports/quality.xml` | 78 passed; zero failures, errors or skips |
| `uv run pytest -q -m infra` | 5 passed |
| `uv run pytest -q -m live -rs` | 3 skipped: no interview provider key; embedding and answerer models not configured |
| `git diff --check` | Passed |

The 78-check gate overlaps the unit/database suites. It includes six fixed
top-one retrieval cases, four supported cited-answer cases and four unsupported
question abstentions, plus fixture acceptance and adversarial boundary checks.
These fixed expectations are test-owned and are never read by the pipeline.

Apply migrations with `uv run eki migrate` before running the updated application.
The operational commands, mail selection example, audit endpoints and provider
setup are documented in `CLAUDE.md`, `mcp_servers/README.md` and `evals/README.md`.

Limits: this is a hardened local synthetic pipeline, not a remote mailbox
integration. Folder/metadata-based exclusions do not classify mislabeled private
prose. Database administrators remain trusted; append-only triggers do not resist
schema alteration. Provider semantic/selection quality and production-scale
performance remain unverified. Audit inserts serialize briefly per schema to
preserve cursor order; this has not been load-tested.
