# Search the runbook and ask a question

**Trigger:** `POST /runbook/search`, `POST /runbook/ask`, `eki search` or `eki ask`.
**Outcome:** results from prepared approved indexes, or exact approved claims with
citations. An unsupported answer is distinct from knowledge still being prepared.
Search never creates indexing jobs or embeds entry content.

## Prepare indexes independently

Apply migration `0019_runbook_index_jobs.sql` with `uv run eki migrate` before
starting the updated API. The API starts one bounded worker per process during
lifespan and waits for its current job on shutdown. For standalone CLI use:

```sh
uv run eki index-runbook                 # long-running worker; Ctrl-C to stop
uv run eki index-runbook --once          # up to 1000 currently due jobs, then exit
uv run eki index-runbook --once --limit 10
```

This is a trusted local operator command using database credentials, like
migrations. It does not need a reader bearer token. A one-shot batch reports
ready/retrying/obsolete counts and exits 1 if a provider/audit failure was deferred.
Failed jobs wait for their due time; run a persistent worker for automatic retries.
No external queue service or new dependency is required.

```mermaid
sequenceDiagram
    actor Reviewer
    participant DB as Postgres
    participant Worker as Index worker
    participant Emb as Embedder
    actor Reader
    participant Search as Search / Ask
    Reviewer->>DB: approve an entry version
    Note over DB: approval + jobs commit together
    Worker->>DB: claim entry/version/model with an advisory lock
    Worker->>Emb: embed redacted sections in batches of 64
    Worker->>DB: lock run and entry; recheck approval/version/content
    Worker->>DB: atomically publish complete index + mark ready
    Reader->>Search: search / ask
    Search->>DB: find permitted prepared approved entries
    Note over Search: return request connection before embedding
    Search->>Emb: embed redacted query only
    Search->>DB: rank current approved versions with live ACLs
    Search-->>Reader: results / cited claims / abstention
```

Approval and substantive entry changes enqueue jobs transactionally for registered
model identities. Drafts, corrections and withdrawals obsolete older jobs and
invalidate indexes immediately. Unchanged reviews and reader flags preserve
embeddings. Workers register the configured model and backfill existing approved
versions; reconciliation repeats every 30 seconds to cover registration/approval
races. Existing valid indexes are preserved, including during migration.

A session advisory lock excludes another worker for the same entry/version/model
throughout provider work, without holding a transaction or request-pool connection.
Its dedicated connection closes on completion or process death. Abandoned
`processing` jobs are reclaimed by the next worker. Different embedding models
have separate jobs and index rows. Retired model registrations remain stored;
only a worker configured for a matching identity processes that model's jobs.

Provider work completes before short run/entry row locks. The worker then checks
that the entry is still approved with the same version and content. Stale work
never publishes; selected vectors and job completion commit together. Provider
and audit errors store only a fixed error code, retrying after 5 seconds with
exponential backoff capped at 300 seconds. An interruption before completion may
repeat provider work on recovery; concurrent workers do not duplicate it.

## Readiness and recovery

`GET /runbook/indexing` returns the configured model, pending count and an
entry-to-state mapping: `pending`, `processing`, `retrying` or `ready`. It includes
only approved entries in runs the caller can currently access. No drafts, private
proposals, worker errors, attempts or provider payloads are exposed. Search JSON
responses also include this readiness metadata.

The browser labels approved entries while preparing or retrying and reports that
results may be incomplete. Entries remain directly readable and exportable during
preparation. If Ask has no supported answer while permitted approved entries
are pending, it returns `reason: indexing`; the browser offers retry guidance
instead of claiming a confirmed knowledge gap or offering to save a follow-up.
This is conservative across the caller's handovers: pending entries may be
unrelated to the question. Retry after preparation completes.

## Ranking and answers

1. Redact and validate the query (1–2000 characters). Only prepared current-version
   indexes belonging to approved, permitted entries qualify. Empty eligible sets
   return without a query provider call.
2. Release the pooled connection, embed the query, then borrow another connection.
   Live SQL approval, version and ACL predicates apply again before ranking.
3. Rank each entry's best keyword/vector chunk and combine ranks with reciprocal
   rank fusion. Vector comparison is exact; its cost grows with approved sections.
4. Ask rereads the hits' approval, version, access and review freshness before and
   after model latency. Overdue entries remain searchable/exportable but are not
   answer evidence. Changed evidence returns `reason: evidence_changed`.
5. Without an answerer model, select relevant exact claims or abstain. With one,
   provide bounded approved evidence as untrusted data and accept only validated
   evidence IDs. The model writes no answer prose and receives no tools.
6. Expand a selected procedure to every prerequisite step in order. If the bounded
   evidence omitted a step, abstain with `reason: context_limit`.
7. Render exact claims and citations. Search/Ask never read the private transcript.
   Only `no_supported_answer` offers an explicit reader follow-up request; overdue
   matches point to review. No search or Ask request creates reader history.

Authentication failures return 401; invalid input returns 422. Query-embedding
failures return 502, while entry-embedding failures remain background retries.
Audit failures prevent provider calls. Search/query text stays in request bodies,
never URLs or logs. API responses are private and uncached.

Implementation: `eki.search.jobs`, `eki.search.index`, `eki.search.service`,
`eki.search.answers`, migration 0019. Coverage: `tests/test_index_jobs.py` and the
search/answer, maintenance, follow-up and browser suites. Real-provider quality
remains unverified by explicit user choice.
