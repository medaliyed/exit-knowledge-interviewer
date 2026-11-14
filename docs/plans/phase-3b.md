# Phase 3, second half: approved runbook retrieval (F11, F12)

Status: authorized by the user's 2026-11-14 instruction to implement the second
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
- [ ] e / P3b: fixture end-to-end acceptance (ranking and useful answers), all
  static/unit/database/infra checks, documentation and honest quality report.

After each task: `uv run pytest -q`, `uv run pytest -q -m db`,
`uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy`.
Finish with `uv run pytest -q -m infra`. Use disposable DB schemas. Report what
is verified and what remains unverified; no claim that offline hashing proves
semantic retrieval or that scripted selectors prove a real model's quality.
