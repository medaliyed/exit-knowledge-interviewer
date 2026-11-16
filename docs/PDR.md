# Exit-Knowledge Interviewer (eki): Project Definition

## Project

"Exit-Knowledge Interviewer" (package name: `eki`). Before an expert retires or
leaves, an agent interviews them systematically. It works from their tickets,
code history and selected emails, asks about the gaps, and turns the answers
into a searchable, cited runbook.

Design: a fixed LangGraph workflow with ONE agentic node (the interviewer).

Flow: Ingest -> Map gaps -> Plan -> Interview -> Synthesise -> Approve -> Searchable runbook.

The expert prunes the interview plan and approves the initial runbook. After an
explicit maintenance handover, the current owner reviews later entry changes.

## Requirements

| ID  | Requirement |
| --- | --- |
| F1  | Read-only connectors for Git history (commits, PRs, reviews) and tickets, scoped to one expert and a date range. |
| F2  | Secrets and personal data are redacted before anything is indexed or sent to a model. |
| F3  | Email connector limited to expert-selected folders; personal/private mail excluded. Introduced in Phase 4. |
| F4  | Cluster artifacts into topics: systems, procedures, incidents, decisions, contacts. |
| F5  | Gap score per topic from: how exclusively the expert handled it, whether docs exist, recency and criticality. |
| F6  | Draft questions per gap, each quoting the artifact that prompted it. Expert can remove, reorder, add topics. |
| F7  | Resumable interview sessions. Follow-ups until the topic template is complete: trigger, diagnosis, steps, rollback, contacts, pitfalls, rationale. |
| F8  | Expert can skip a question, mark an answer confidential, or correct the agent. |
| F9  | Runbook entries follow one template. Every procedural claim cites an artifact or an interview turn. Uncited claims block publication. |
| F10 | The expert approves, edits or rejects entries during capture; after explicit maintenance handover the current owner reviews later changes. Nothing is published without approval. |
| F11 | Hybrid search (keyword + vector) and Q&A over approved entries only, with citations and ACL filtering. |
| F12 | Export approved entries as Markdown. |
| F13 | Audit log of every model call, tool call and approval with run ID and user. |
| F14 | Durable interview message receipts, explicit delivery status and safe retries after crashes, without lost accepted answers or duplicate logical effects. Delivered: Phase 5a. |
| F15 | Recorded real-model acceptance evidence for interview behavior, semantic retrieval and grounded answers, with explicit provider opt-in. Deferred by user request: Phase 5b. |
| F16 | An authenticated browser workflow for plan review, interview chat, cited entry review and approval. Implemented: Phase 5c; visual browser QA pending. |
| F17 | Optional semantic retrieval over source artifacts, with matching document/query model identities and an offline fallback. Implemented: Phase 5d; real-model quality pending. |
| F18 | Resumable plan generation that retains completed topics and retries unfinished work under a stable generation ID. Delivered: Phase 5e. |
| F19 | Invited work-email/password accounts, bounded server-side browser sessions, one-time password links, local operator account management, CSRF protection, throttling and metadata-only authentication audit. Phase 6. |
| F20 | Golden interview evaluations: strict scenario/golden files, isolated real-service runs, shared model-call budgets, deterministic scorecards and comparable redacted reports. Phase 7; paid evidence pending. |
| F21 | Maintain knowledge after the expert leaves: explicit successor ownership and attributed re-approval, entry review-by dates, freshness across retrieval/export, and reader flags with owner triage. Implemented: Phase 8. |
| F22 | Focused follow-up interviews while the original expert is reachable. Phase 9a/9b implement gap intake, reviewed focused sessions and durable budgets. Entry proposals/source lineage and browser/CLI journeys remain open in 9c/9d. |

## Stack

- Python 3.12 with uv.
- FastAPI (async, SSE).
- LangGraph 1.x with Postgres checkpointer.
- Postgres 16 + pgvector.
- MCP Python SDK, one read-only server per source (git, tickets, mail).
- LiteLLM as model gateway, model names from config.
- Langfuse self-hosted via Docker for tracing.
- pytest, ruff, mypy.
- Docker Compose for local infra.

## Delivery phases

| Phase | Name | Scope |
| --- | --- | --- |
| 0 | Frame | Scaffold, infra, runbook template, synthetic fixtures. |
| 1 | Ingest and map | Git + ticket MCP servers, redaction, pgvector, clustering, gap scoring. |
| 2 | Interview | Plan review, LangGraph interviewer with checkpoints, chat API, tracing. |
| 3 | Runbook | Synthesis with citations, approval flow, hybrid search, export. |
| 4 | Harden | Mail connector, red-team suite, evals in CI, audit log. |
| 5 | Recovery and expert workflow | [Phase 5 PDR](plans/phase-5.md): reliable interview recovery, real-model validation, expert UI, semantic artifact retrieval and resumable plan drafting, in that order. |
| 6 | Email/password accounts | [Phase 6 PDR](plans/phase-6.md): account provisioning, sessions/authentication, browser sign-in and operational documentation. |
| 7 | Golden interview evals | [Phase 7 PDR](plans/phase-7.md): bounded real-model interviews, deterministic grading, saved reports and comparisons; owner-review drafts for G1–G8. |
| 8 | Knowledge maintenance | [Phase 8 PDR](plans/phase-8.md): successor handover, review dates and reader flags. Implemented. |
| 9 | Follow-up interviews | [Phase 9 PDR](plans/phase-9.md): gap intake and bounded focused sessions implemented (9a/9b); cited entry proposals and browser/CLI journeys pending (9c/9d). |

Each phase is planned separately in `docs/plans/phase-N.md` and needs human
approval before implementation starts. The hard rules that apply to every
phase are in `CLAUDE.md`. The synthetic test corpus is specified in
`docs/fixtures-spec.md`.

## Status

Phases 0–4 are implemented. Phase 5 code deliverables are implemented;
real-provider acceptance is deferred by user request and visual browser QA is pending.
The plan files record scope, approval and verification status in their Status
line and, where there is one, their Outcome section.

| Phase | Plan | Requirements and status |
| --- | --- | --- |
| 0 Frame | `docs/plans/phase-0.md` | scaffold, infra, template, fixtures |
| 1 Ingest and map | `docs/plans/phase-1.md` | F1, F2, F4, F5 |
| 2 Interview | `docs/plans/phase-2.md` | F6, F7, F8 |
| 3 Runbook | `docs/plans/phase-3.md`, `docs/plans/phase-3b.md` | F9, F10, F11, F12 |
| 4 Harden | `docs/plans/phase-4.md` | F3, F13, red-team and quality gate |
| 5 Recovery and expert workflow | [docs/plans/phase-5.md](plans/phase-5.md) | F14/F16/F17/F18 implemented; F15 live evidence and manual screen-reader validation pending |
| 6 Email/password accounts | [docs/plans/phase-6.md](plans/phase-6.md) | F19 implemented 2026-11-15; manual browser pass and timing check pending |
| 7 Golden interview evals | [docs/plans/phase-7.md](plans/phase-7.md) | F20 implemented 2026-11-15; draft adoption and paid model evidence pending |
| 8 Knowledge maintenance | [docs/plans/phase-8.md](plans/phase-8.md) | F21 implemented 2026-11-15; successor review, freshness and reader feedback |
| 9 Follow-up interviews | [docs/plans/phase-9.md](plans/phase-9.md) | F22: 9a/9b implemented 2026-11-16; 9c/9d pending |

Added after the phases: synthesis keeps drafts the expert edited by hand
(F10), review lists claims that say more than the answer they cite (F9), and
synthesis and corrections share an abbreviation-aware procedure step splitter
(F9).

Phase 5 starts with F14: an accepted answer must survive a process restart
without retyping or duplicate turns. Its PDR defines five ordered deliverables,
acceptance criteria, compatibility requirements and completion evidence.
Implementation was approved on 2026-11-14. Real-provider runs still require
explicit provider configuration and a call/token budget.

How it is built and how it behaves at runtime: `docs/architecture.md` and
`docs/flows/`. How to run it: `README.md` and, for a first run, `docs/tutorial.md`.

Still open:

- [Phase 9](plans/phase-9.md) now supports gap intake and focused follow-up sessions
  through the HTTP/shared services. Mapped entry proposals, source lineage and
  dedicated browser/CLI journeys remain open in 9c/9d.
- No run against a real model yet. Interview quality, semantic search and
  model-selected answers are unverified; Phase 5b will record actual evidence.
- Durable message recovery and per-topic plan recovery are implemented. Calls
  interrupted before a durable result may still repeat and be billed again.
- The expert browser UI is implemented and HTTP-verified; visual browser QA is
  pending because browser control is unavailable. Semantic source-artifact
  retrieval is implemented, with real-provider quality pending by user request.
- The connectors read the synthetic local corpus only; there is no adapter for
  a live mailbox, Git host or issue tracker.
- `evals/scenarios/` and `evals/golden/` are human-owned and do not exist yet;
  the CI quality gate runs the synthetic contracts in `tests/`. Phase 7 supplies
  eight review drafts under `docs/plans/phase-7-drafts/` and a separate opt-in
  grader; it does not adopt the drafts or change CI.
- Known gaps in the implementation are listed in `docs/flows/index.md`.
