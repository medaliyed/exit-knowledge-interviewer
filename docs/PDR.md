# Exit-Knowledge Interviewer (eki): Project Definition

## Project

"Exit-Knowledge Interviewer" (package name: `eki`). Before an expert retires or
leaves, an agent interviews them systematically. It works from their tickets,
code history and (later) emails, asks about the gaps, and turns the answers
into a searchable, cited runbook.

Design: a fixed LangGraph workflow with ONE agentic node (the interviewer).

Flow: Ingest -> Map gaps -> Plan -> Interview -> Synthesise -> Approve -> Searchable runbook.

The expert prunes the interview plan and approves every runbook entry.

## Requirements

| ID  | Requirement |
| --- | --- |
| F1  | Read-only connectors for Git history (commits, PRs, reviews) and tickets, scoped to one expert and a date range. |
| F2  | Secrets and personal data are redacted before anything is indexed or sent to a model. |
| F3  | Email connector limited to expert-selected folders; personal/private mail excluded. LAST PHASE ONLY. |
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

## Stack

- Python 3.12 with uv.
- FastAPI (async, SSE).
- LangGraph 1.x with Postgres checkpointer.
- Postgres 16 + pgvector.
- MCP Python SDK, one read-only server per source (git, tickets, later mail).
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

Each phase is planned separately in `docs/plans/phase-N.md` and needs human
approval before implementation starts. The hard rules that apply to every
phase are in `CLAUDE.md`. The synthetic test corpus is specified in
`docs/fixtures-spec.md`.
