# Phase 5 PDR: recovery and expert workflow

**Status:** implementation approved on 2026-11-14 by the user's instruction
to continue implementation. Deliverables are tracked below; real-provider
validation requires explicit provider configuration and a call/token budget.

**Parent:** [Project PDR](../PDR.md), requirements F14–F18. This extends F6–F11
and retains the existing redaction, confidentiality, approval and audit rules.

**Goal:** make the interview dependable through interruptions, verify its
behavior with real models, and let an expert complete the workflow in a browser.
Then improve source retrieval and recovery during plan generation.

## Problem and current baseline

Phases 0–4 provide the complete pipeline against synthetic sources. The core
workflow, access rules and publication boundaries have automated coverage, but
several gaps prevent a dependable expert experience:

| Gap observed in the current implementation | Consequence | Deliverable |
| --- | --- | --- |
| If no pending interrupt is found, `InterviewService._accept` recovers the workflow without accepting the submitted message. | The expert may need to retype an answer after a crash. | 5a / F14 |
| Existing live tests have not produced real-provider acceptance evidence. | Scripted doubles establish workflow behavior, but interview and semantic quality remain unverified. | 5b / F15 |
| CLI and API exist, but no browser workflow does; plan pruning requires the API. | An expert needs technical help to complete the process. | 5c / F16 |
| Artifact ingestion and interviewer retrieval use the local hashing embedder. | Paraphrases without shared terms may miss relevant tickets or commits. | 5d / F17 |
| Plan questions are saved only after every topic is drafted; a model failure deletes the workflow thread. | Retrying a late failure repeats completed model work. | 5e / F18 |

Sources: [interview flow](../flows/interview-turn.md),
[draft-plan flow](../flows/draft-plan.md),
[ingest flow](../flows/ingest.md), `src/eki/interview/service.py`,
`src/eki/graph/workflow.py`, `src/eki/ingest/index.py`,
`tests/test_live_model.py` and `tests/test_live_retrieval.py`.

## Scope and delivery order

| Order | Requirement | Deliverable | Completion evidence |
| --- | --- | --- | --- |
| 5a | F14 | Durable message acceptance and recovery | Crash/retry tests, API/CLI status and migration compatibility |
| 5b | F15 | Real-model acceptance evidence | Recorded results for configured interview, retrieval and answerer roles |
| 5c | F16 | Expert browser workflow | A complete synthetic interview, review and export through the UI |
| 5d | F17 | Semantic artifact retrieval | Model-isolated indexing, paraphrase retrieval and offline regression checks |
| 5e | F18 | Resumable plan generation | A late failure retries only unfinished topics |

5a comes first. 5b uses the existing live tests before adding new evaluation
infrastructure. 5c builds on the delivery contract from 5a. 5d adds its own live
artifact-retrieval checks to the evidence established in 5b. 5e reuses the retry
and status conventions from 5a. Missing provider configuration leaves live
validation incomplete; independent implementation work can continue, but a skip
must not be reported as a successful quality check.

This phase keeps the current Python/FastAPI/Postgres/LiteLLM stack and one
agentic workflow node. The UI starts with FastAPI-served HTML, CSS and browser
JavaScript using the same-origin API. No new dependency is assumed. Live source
connectors, a new account/login system, background runbook indexing and
large-corpus optimization are outside this phase.

## 5a — Durable interview message acceptance (F14)

### User outcome

An expert submits an answer, sees that it was saved, and can resume after a
disconnect or process restart without retyping it. A retry cannot create another
logical answer or apply the same correction twice.

### Requirements

- Each logical submission has a stable client-generated message ID. Answers and
  skips name their intended question turn; corrections name the turn they
  correct. Retries retain the ID, action, target and confidentiality flag.
- Authenticate and validate the target before acceptance. Redact before durable
  storage. A message is acknowledged as saved only after its receipt and
  protected payload or transcript reference have committed.
- Deduplicate within the session. Reusing an ID with the same canonical,
  redacted payload returns the existing receipt/result. A different action,
  target, confidentiality flag or redacted text under that ID returns a conflict
  without replacing the accepted submission.
- Expose receipt states `saved`, `processing`, `needs_retry` and `processed`.
  A stale or invalid target is refused explicitly; never silently attach an
  answer to the question that happens to be current after recovery.
- A second, different submission for an already answered question conflicts
  rather than overwriting its answer. Changes to accepted answers use the
  explicit correction action.
- Recover an interrupted workflow and reconcile the accepted message with its
  original question and turn before processing it. Recover abandoned processing
  ownership after a process dies; serialize concurrent submissions per session.
- Replaying work must not duplicate turns, active notes, correction decisions,
  question turns or synthesized entries. Return a stable result reference when
  processing already completed, even if the client missed the terminal SSE event.
- CLI and HTTP clients use the same acceptance contract. CLI retries retain the
  submission ID. New clients show saved/processing/retry status and can resume
  from a receipt instead of submitting the answer again.
- Existing callers may omit the new ID and question fields during a documented
  compatibility period. Return the assigned ID; document that deduplication
  across transport retries requires reuse of that ID. Reject ambiguous targets
  rather than guessing during crash recovery.
- Receipt/status reads use owner/admin access, matching session routes. Readers
  cannot inspect pending answers. Confidential payloads remain excluded from
  model input, traces, workflow checkpoints, status responses and synthesis.
  Prefer references to protected transcript data over another copy of the text.
- Audit delivery transitions with IDs, status and counts only. Database effects
  are idempotent; a provider call interrupted before a durable result may be
  repeated and billed. Do not promise exactly-once provider execution.

The first release resumes accepted work when the client reconnects or explicitly
retries; it does not require an always-on background worker. Status responses
must distinguish a persisted answer from completed model processing.

### Acceptance criteria

1. Terminate the process after acceptance commits but before acknowledgement.
   Reconnect and retry the same ID: the answer exists once and processing resumes.
2. Terminate after the expert turn is written, during model/tool processing and
   after completion but before the final SSE event. Each restart preserves the
   intended question, one logical answer and one set of resulting state changes.
3. Exercise answer, skip, confidential answer and correction paths, including a
   correction that reopens an approved entry. Replaying the correction does not
   add another decision or increment the entry version again.
4. Concurrent retries of one ID resolve to one receipt/result. Changed payloads,
   stale questions and unauthorized receipt reads fail explicitly.
5. A model/audit outage reports retryable processing without claiming the saved
   answer was lost. A storage failure before acceptance never reports `saved`.
6. Previously created sessions and existing scripted-model acceptance tests
   continue to work after migration.

**Primary acceptance:** interrupt the process mid-interview, restart it, and
recover the saved answer without retyping or duplicate turns.

## 5b — Real-model acceptance evidence (F15)

Use `tests/test_live_model.py` and `tests/test_live_retrieval.py` first. Address
failures with reproducible regressions; add cases only where existing checks
leave a behavior untested. Use synthetic sources and disposable schemas.

Before any paid run, the operator explicitly selects model roles, exports
credentials and sets a call/token budget. Default tests and CI remain
provider-free. Record revision, model identifiers, configuration, executed and
skipped cases, token usage, duration and outcomes in a reproducible report.
Report only metadata and safe synthetic examples; never credentials or
confidential answers. Keep human-owned `evals/scenarios/` and `evals/golden/`
untouched.

Acceptance requires actual provider results for all of the following:

- The interviewer follows up on the missing G3 rollback, leaves unknown G7
  sections open and presents the G1 contradiction to the expert.
- Supported operator paraphrases retrieve the expected approved runbook entry.
- The answerer selects supported cited evidence and abstains on unsupported
  questions; procedure answers preserve their full order.
- Each selected configuration passes the applicable checks in three recorded
  runs. Preserve failures and reruns rather than reporting only the best run.

These are bounded acceptance checks, not proof of broad interview or retrieval
quality. Missing credentials or model configuration means validation remains
unverified; it does not satisfy F15.

## 5c — Expert browser workflow (F16)

Provide a small same-origin UI over the existing authenticated API:

1. Open an accessible run/plan; see ranked topics and their source quotations.
   Remove, reorder or add topics, then explicitly approve the plan.
2. Answer interview questions with streamed updates. Support skip, confidential
   answers and corrections, with delivery status and recovery from 5a.
3. Review the seven-section draft beside permitted source turns, citations,
   open questions and advisory unsupported-claim flags. Edit, reject or approve
   each entry explicitly.
4. Search approved entries, ask cited questions and download approved Markdown.

Use existing bearer identities and owner/reader rules. Credentials remain in
memory, not URLs or browser persistent storage. Recover saved delivery state
from the server; do not persist answer text or confidential content in browser
storage. Add only the scoped listing/status endpoints needed for navigation.
Readers see approved entries, not transcripts or review controls. Render source
and model text as untrusted text. Include keyboard navigation, labelled controls,
visible focus, progress and actionable error messages.

Acceptance: an expert completes a synthetic run from plan pruning to export
without using the CLI or raw API. Reload/disconnect recovery retains the saved
answer and its delivery state. Reader/owner views enforce the same access rules
as the API; approval remains explicit and confidential transcript text is never
returned or displayed after submission.

## 5d — Semantic source-artifact retrieval (F17)

Extend the optional semantic capability to artifact ingestion and the
interviewer's `search_artifacts` path. Runbook embedding configuration already
exists; configuring it currently does not make artifact retrieval semantic.

- Introduce a separately documented artifact embedding model setting. Keep
  deterministic local hashing as the default offline mode, clearly labelled.
- Reuse the existing embedding interface/provider validation. Retain 256
  dimensions initially; refuse incompatible output rather than truncating it.
- Persist the model identity used for each artifact index. Embed queries with
  that same model. Isolate model spaces and provide an explicit reindex command
  when configuration changes; never compare vectors from different models.
- Bound provider batches, redact before embedding and audit provider attempts.
  Publish a replacement index atomically; a failed reindex leaves the previous
  usable model/index pair intact. Changing configuration must not query an old
  index with the new model.
- Preserve run scoping, source IDs and citations. Existing chunks migrate as
  the known offline hashing model. Do not silently re-embed historical runs.

Acceptance: offline regression tests remain green; scripted provider checks
cover model isolation, dimension validation, redaction, run isolation and
interrupted reindexing. Opt-in real-provider checks use at least three fixed
synthetic paraphrases spanning different topics, with expected artifact IDs
recorded before the run. All three must return an expected artifact in the top
five in each of three runs; report hashing and semantic results separately.

## 5e — Resumable plan generation (F18)

Give each generation attempt a stable ID and persist its selected topics,
ordering, input revision and per-topic completion. Store validated questions,
contradictions and dropped-question reasons after each completed topic.

Resume the same generation after failure, retaining completed topic results
and processing only unfinished topics. Keep explicit restart as a separate
operation that creates a new generation. Serialize concurrent resumes. Changed
inputs or model/prompt configuration require an explicit new generation rather
than mixing results silently.

Expose progress and retryable failure through CLI/API and the UI. Partial work
is inspectable by the owner but cannot be approved until generation completes.
Preserve the existing draft/approved plan behavior after completion. Retries
retain audit history, without guaranteeing the avoidance of a provider call
whose result was never persisted.

Acceptance: fail topic 9 of a ten-topic generation, restart the service and
resume. Topics 1–8 retain exactly their saved questions and receive no further
drafting calls; topics 9–10 finish under the same generation ID. Repeated resumes
do not duplicate questions or plans. Concurrency, changed-input, partial-plan
approval and cross-run access tests pass.

## Engineering and rollout

Use additive migrations for receipts, artifact model identity and generation
progress. Keep old data readable. Define API fields and status/error contracts
before their clients; document compatibility and migration commands. Check the
installed, pinned LangGraph/checkpointer APIs before changing recovery. Its
[interrupt guidance](https://github.com/langchain-ai/docs/blob/main/src/oss/langgraph/interrupts.mdx)
explains why replayed side effects require idempotency.

For each deliverable, write the regression/acceptance checks first and commit
verified work with its requirement ID. Update the affected runtime flow pages
when behavior changes; this PDR describes planned behavior only.

- [x] 5a / F14: receipt storage, target binding, recovery, API/CLI status and crash tests.
- [ ] 5b / F15: existing live checks executed, failures addressed, repeated-run evidence recorded.
- [ ] 5c / F16: plan/chat/review/search/export UI and browser/access acceptance checks.
- [ ] 5d / F17: artifact model identity, semantic indexing/querying, reindexing and live retrieval evidence.
- [ ] 5e / F18: per-topic drafting persistence, resume/status operations and failure tests.
- [ ] P5-final: migration, end-to-end acceptance, updated documentation and honest outcome report.

Run the existing unit/database suites, Ruff lint/format checks, mypy and the
quality/privacy gate after implementation changes. Add focused browser checks
for 5c and explicit opt-in live checks for 5b/5d. Use disposable schemas and keep
provider calls out of default CI. Documentation-only changes require link,
consistency and diff checks rather than another runtime test run.

Phase 5 is complete only when its deliverables meet their acceptance criteria
and the outcome report identifies the tested configurations and remaining
limits. A completed UI or a passing scripted suite does not substitute for
the real-provider evidence required by F15 and F17.


## Delivery evidence

### 5a / F14 — implemented 2026-11-14

Receipts, protected transcript references, API/CLI status/resume, stored redacted
model replies and idempotent note/synthesis replay are implemented. Migration
`0009_delivery.sql` is additive. Existing no-ID callers retain their SSE progress
shape; unsafe ambiguous submissions now conflict instead of being discarded.

Verification: 428 unit checks, 200 database checks and the 78-check quality gate
passed, followed by all 11 focused delivery checks after adding two actual
process-death cases. Those terminate a separate process before acknowledgement
or after its first note commit, then resume the saved answer once. Ruff lint,
format and strict mypy passed. Provider calls remained disabled.
