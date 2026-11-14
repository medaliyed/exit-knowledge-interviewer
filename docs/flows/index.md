# Runtime flows

What `eki` does at runtime, one page per flow. Each page has a sequence
diagram, a walkthrough that cites the code, the failure modes and the things
that are worth knowing before changing it.

For the static picture (pipeline, components, workflow graph, data model) see
[../architecture.md](../architecture.md).

| Flow | Trigger | In one line |
| --- | --- | --- |
| [Ingest one expert's work](ingest.md) | `eki ingest` | Read sources through read-only MCP servers, redact twice, store and index a run atomically. |
| [Draft an interview plan](draft-plan.md) | `eki plan`, `POST /runs/{id}/plan` | Pick the highest-gap topics and have a model draft questions that must quote an artifact. |
| [Message delivery and recovery](message-delivery.md) | Message POST or receipt resume | Save a question-bound message before processing and resume without repeating logical effects. |
| [One interview turn](interview-turn.md) | `eki interview`, `POST /sessions/{id}/messages` | Store the answer, let the interviewer model note it and ask again, checkpoint at every question. |
| [Review and approve an entry](review-entry.md) | `eki review`, `POST /entries/{id}/approve` | Show the draft with its unsupported claims, then approve it under lock with a decision and an audit event. |
| [Search and ask](search-and-ask.md) | `eki search`, `eki ask`, `/runbook/search`, `/runbook/ask` | Index approved entries lazily, rank with keyword and vector search under access rules, answer with exact claims or abstain. |

The flows run in this order for one expert. `eki map` sits between the first
two; it is described at the end of [ingest.md](ingest.md).

## Triggers without a page

These are single-component operations. A sentence is enough.

| Trigger | What it does |
| --- | --- |
| `eki migrate` | Applies the SQL migrations in order and creates the workflow checkpoint tables. |
| `eki map`, `eki gaps` | Builds topics and gap scores from a stored run; prints the ranking. No model, no source access. |
| `GET/PATCH /plans/{id}` | Reads a plan; removes, adds or reorders topics until it is approved. |
| `POST /plans/{id}/approve` | Freezes the plan, opens its session and runs the workflow to the first question. Covered in [draft-plan.md](draft-plan.md) and [interview-turn.md](interview-turn.md). |
| `GET /sessions/{id}` | Returns the transcript, notes and open items. Confidential text is replaced by a marker. |
| `POST /sessions/{id}/synthesise` | Rebuilds draft entries from the notes; approved, rejected and hand-edited entries are kept. |
| `PATCH /entries/{id}`, `POST /entries/{id}/reject` | Variants of the review flow, described in [review-entry.md](review-entry.md). |
| `eki export`, `GET /entries/{id}/markdown`, `GET /runbook/export` | Renders approved, permitted entries as Markdown. One read, no model. |
| `eki access`, `/runs/{id}/owner`, `/runs/{id}/access` | Sets a run's owner and readers, with an audit event in the same transaction. |
| `eki audit`, `GET /runs/{id}/audit` | Pages through a run's audit events by ascending ID. |
| `GET /healthz` | Returns `{"status": "ok"}`. No authentication, no database. |

## Known gaps, collected

Things the flow pages flag. Each is cited on its page.

- **Approval is bound to a display name**, not to an account.
  ([review-entry.md](review-entry.md))
- **A failed plan draft discards all drafted questions**; a retry pays for
  them again. ([draft-plan.md](draft-plan.md))
- **Real-provider calls interrupted before their result is persisted may repeat**
  on recovery. Accepted messages and stored replies survive.
  ([message-delivery.md](message-delivery.md))
- **Indexing happens on the search path** and the vector comparison is exact,
  so cost grows with the number of approved sections.
  ([search-and-ask.md](search-and-ask.md))
- **The artifact index is lexical** whatever embedding model is configured.
  ([ingest.md](ingest.md))
- **Nothing here has run against a real model or a real source.** All flows
  were traced by reading the code and are exercised by tests with scripted
  model doubles and the synthetic corpus.
