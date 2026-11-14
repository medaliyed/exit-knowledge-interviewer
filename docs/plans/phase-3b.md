# Phase 3, second half: approved runbook retrieval (F11, F12)

Status: implemented and verified on 2026-11-14. Authorized by the user's
2026-11-14 instruction to implement the second
half with good quality. This plan makes the implementation choices concrete;
that instruction authorizes proceeding without another approval round.

## Decisions and scope

- No new dependencies. Use FastAPI, Postgres full-text search, pgvector and the
  existing LiteLLM/model interfaces. No additional agentic workflow node.
- Bearer credentials come from `EKI_API_TOKENS` (secret JSON configuration).
  Identity is a stable subject plus display name, with an explicit admin role.
  Missing/invalid credentials fail closed. `/healthz` remains public.
- Runs have an owner and a reader ACL. Existing runs are unassigned/admin-only
  until an operator assigns ownership. Owners/admins manage interview data;
  readers retrieve approved entries only. Review actor names must match the
  authenticated identity as well as the run's expert. Every data API route is
  protected, including existing session, transcript and draft-entry routes.
- Local CLI ingest/interview/review/access administration uses trusted local DB
  operator privileges. New search, ask and export commands require an explicit
  configured bearer identity (`EKI_API_TOKEN`); no subject impersonation flag.
- Index approved entries only. DB triggers invalidate index rows whenever entry
  content/version/status changes. Retrieval joins the live approval/version and
  ACL before either ranking channel. Refresh only accessible approved entries.
  Deterministic reciprocal rank fusion and stable ties combine keyword/vector
  rankings. No result cache shared between users.
- Keep local 256-dimensional hashing embeddings for offline operation. Add an
  optional LiteLLM semantic embedder, validate dimensions and finite values, and
  isolate indexes by embedding model identity. Document the offline limitation.
- Q&A assembles exact source claims and their citations. An optional configured
  answerer selects evidence IDs, never unrestricted answer prose. Without a
  model, deterministic extraction ranks claims and abstains on weak evidence.
  Reject malformed/unknown model citations and tool calls. Treat source text as
  untrusted data, not instructions. Recheck access/version after model latency;
  discard answers whose evidence became unavailable. Do not expose transcript
  text through citations or fetch inaccessible content for a model.
- Markdown export uses the same live approved-entry/ACL checks, includes all
  seven sections, metadata, open questions and source footnotes, and escapes
  untrusted Markdown/HTML. No export of drafts/rejected entries. CLI file output
  refuses to overwrite an existing file.
- Redact queries before embedding/model calls; redact again at index/render
  boundaries. No credential defaults, raw credentials in logs, fixture key
  reads in production, real-model tests by default, or edits to human evals.

## Tasks (test first, one requirement-scoped commit per deliverable)

- [x] a / F11: migration and authentication/authorization, all existing API route
  protection, actor binding, operator ACL CLI, negative tests for missing/bad
  credentials, cross-user reads/writes, draft/transcript access and ACL updates.
- [x] b / F11: approved-only versioned search index, invalidation, provider/offline
  embeddings, filtered hybrid ranking, search API/CLI, relevance and isolation
  tests including immediate edit/correction/revocation visibility changes.
- [x] c / F11: cited extractive Q&A, optional evidence selector, answer API/CLI,
  abstention, injection/forged citations/redaction and in-flight revocation tests.
- [x] d / F12: safe Markdown renderer and gated API/CLI exports; citations,
  complete template, open questions, approval/access and file safety tests.
- [x] e / P3b: fixture end-to-end acceptance (ranking and useful answers), all
  static/unit/database/infra checks, documentation and honest quality report.

After each task: `uv run pytest -q`, `uv run pytest -q -m db`,
`uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy`.
Finish with `uv run pytest -q -m infra`. Use disposable DB schemas. Report what
is verified and what remains unverified; no claim that offline hashing proves
semantic retrieval or that scripted selectors prove a real model's quality.


## Outcome

Implemented F11 and F12, including protection of all earlier data routes. No new
dependency or additional agentic workflow node was added. Credential registries
fail closed; unassigned runs are admin-only. Local operator CLI access remains
separate from HTTP reader access and is documented explicitly.

### Quality and isolation evidence

- `tests/test_phase3b_acceptance.py`: the real synthetic fixture interview with
  process restarts, explicit approvals, a confidential filler answer and a
  planted secret. Three fixed queries rank the expected topic first (3/3), and
  three operator questions return claims from the expected topic and section
  (3/3). Export includes public open questions and citations; confidential
  answers/secrets never appear in the index or exported content. Revocation
  immediately removes search and export visibility.
- `tests/test_auth.py`, `tests/test_api_access.py`: every data endpoint requires
  authentication; OpenAPI documents bearer auth. Invalid/duplicate/conflicting
  credentials fail closed. Cross-run, draft, transcript, review-history, actor
  impersonation, ownership transfer and ACL-update cases are covered.
- `tests/test_runbook_search.py`, `tests/test_search_embedding.py`: stable keyword
  ranking, vector-only matches with an injected semantic test embedder, separate
  model spaces, valid dimensions/finite values, redacted provider inputs,
  live edit/correction invalidation and cross-run ACL filtering. A provider
  failure after the first 64 chunks leaves no partial entry index and retries
  successfully to all 130 procedure chunks. A 500-claim entry cannot starve
  another matching entry: candidates are deduplicated per entry before ranking.
- `tests/test_runbook_answers.py`: useful exact claims and abstention; full step
  ordering/prerequisites; forged IDs, prose and tool calls rejected; irrelevant
  vector candidates do not force an answer; denied content never reaches the
  selector; in-flight permission changes and edit/re-approval suppress stale
  evidence. All-open entries produce open questions, never invented procedures.
- `tests/test_runbook_export.py`, `tests/test_cli.py`: safe complete Markdown,
  deduplicated citations, live approval/access gates, authenticated CLI search,
  answer and export, bulk filtering, and refusal to overwrite existing files.

Quality checks exposed three issues fixed with regression tests: blending
unrelated procedures sharing a system name, committing partial indexes after
interrupted provider batches, and a long entry crowding another matching entry
out of chunk-level candidate limits. Extraction now uses the best-supported
entry and conservative overlap; indexing is atomic; fusion ranks entries after
selecting each channel's best chunk.

### Limits

These are small fixed synthetic relevance checks, not a broad relevance
benchmark. Offline hashing is not semantic understanding. Provider adapters and
model evidence selection were tested with doubles; no paid/live provider call
was made. Exact filtered vector search favors correctness at this corpus size;
large-corpus latency and production load are not measured. A real-model note's
faithfulness remains a phase-2 quality concern. Identity is configured bearer
tokens rather than SSO; rotation requires a service restart. No changes were
made to `evals/scenarios/` or `evals/golden/`.

### Implementation references checked

- [PostgreSQL 16 text search controls](https://www.postgresql.org/docs/16/textsearch-controls.html)
- [pgvector exact search and filtering](https://github.com/pgvector/pgvector)
- [LiteLLM embeddings, dimensions and response format](https://docs.litellm.ai/docs/embedding/supported_embedding)


### Final verification

- `uv run pytest -q`: **343 passed** (157 deselected).
- `uv run pytest -q -m db`: **151 passed** (349 deselected), disposable schemas.
- `uv run ruff check .`: passed.
- `uv run ruff format --check .`: 208 files formatted.
- `uv run mypy`: passed, 127 source/test files checked.
- `uv run pytest -q -m infra`: **5 passed** (495 deselected).

All five tasks are complete. Apply `uv run eki migrate`, configure bearer
identities and assign run ownership before using the protected API. See
`CLAUDE.md` and `.env.example` for operation and configuration.
