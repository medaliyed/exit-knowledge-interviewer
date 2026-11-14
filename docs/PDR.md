# Exit-Knowledge Interviewer (eki): Project Definition

## Project

"Exit-Knowledge Interviewer" (package name: `eki`). Before an expert retires or
leaves, an agent interviews them systematically. It works from their tickets,
code history and selected emails, asks about the gaps, and turns the answers
into a searchable, cited runbook.

Design: a fixed LangGraph workflow with ONE agentic node (the interviewer).

Flow: Ingest -> Map gaps -> Plan -> Interview -> Synthesise -> Approve -> Searchable runbook.

The expert prunes the interview plan and approves every runbook entry.

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
| F10 | Expert approves, edits or rejects each entry. Nothing is published without approval. |
| F11 | Hybrid search (keyword + vector) and Q&A over approved entries only, with citations and ACL filtering. |
| F12 | Export approved entries as Markdown. |
| F13 | Audit log of every model call, tool call and approval with run ID and user. |
| F14 | Durable interview message receipts, explicit delivery status and safe retries after crashes, without lost accepted answers or duplicate logical effects. Delivered: Phase 5a. |
| F15 | Recorded real-model acceptance evidence for interview behavior, semantic retrieval and grounded answers, with explicit provider opt-in. Planned: Phase 5b. |
| F16 | An authenticated browser workflow for plan review, interview chat, cited entry review and approval. Implemented: Phase 5c; visual browser QA pending. |
| F17 | Optional semantic retrieval over source artifacts, with matching document/query model identities and an offline fallback. Planned: Phase 5d. |
| F18 | Resumable plan generation that retains completed topics and retries unfinished work under a stable generation ID. Planned: Phase 5e. |

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

Each phase is planned separately in `docs/plans/phase-N.md` and needs human
approval before implementation starts. The hard rules that apply to every
phase are in `CLAUDE.md`. The synthetic test corpus is specified in
`docs/fixtures-spec.md`.

## Status

Phases 0–4 are implemented. Phase 5 is approved and in progress.
The plan files record scope, approval and verification status in their Status
line and, where there is one, their Outcome section.

| Phase | Plan | Requirements and status |
| --- | --- | --- |
| 0 Frame | `docs/plans/phase-0.md` | scaffold, infra, template, fixtures |
| 1 Ingest and map | `docs/plans/phase-1.md` | F1, F2, F4, F5 |
| 2 Interview | `docs/plans/phase-2.md` | F6, F7, F8 |
| 3 Runbook | `docs/plans/phase-3.md`, `docs/plans/phase-3b.md` | F9, F10, F11, F12 |
| 4 Harden | `docs/plans/phase-4.md` | F3, F13, red-team and quality gate |
| 5 Recovery and expert workflow | [docs/plans/phase-5.md](plans/phase-5.md) | F14–F18; approved, in progress |

Added after the phases: synthesis keeps drafts the expert edited by hand
(F10), review lists claims that say more than the answer they cite (F9), and
synthesis and corrections share an abbreviation-aware procedure step splitter
(F9).

Phase 5 starts with F14: an accepted answer must survive a process restart
without retyping or duplicate turns. Its PDR defines five ordered deliverables,
acceptance criteria, compatibility requirements and completion evidence. Implementation was approved on 2026-11-14. Real-provider runs still require
explicit provider configuration and a call/token budget.

How it is built and how it behaves at runtime: `docs/architecture.md` and
`docs/flows/`. How to run it: `README.md` and, for a first run, `docs/tutorial.md`.

Still open:

- No run against a real model yet. Interview quality, semantic search and
  model-selected answers are unverified; Phase 5b will record actual evidence.
- Durable message recovery is implemented in Phase 5a; failed plan drafts still
  discard completed topics until Phase 5e is delivered.
- The expert browser UI is implemented and HTTP-verified; visual browser QA is
  pending because browser control is unavailable. Source-artifact retrieval still
  uses lexical hashing until Phase 5d.
- The connectors read the synthetic local corpus only; there is no adapter for
  a live mailbox, Git host or issue tracker.
- `evals/scenarios/` and `evals/golden/` are human-owned and do not exist yet;
  the CI quality gate runs the synthetic contracts in `tests/`.
- Known gaps in the implementation are listed in `docs/flows/index.md`.
