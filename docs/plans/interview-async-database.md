# F7/F14: keep interview database work off the event loop

Authorized by the user on 2026-11-17 as review point 2.

Move synchronous connection acquisition, dependency queries and connection
cleanup in the async interview service into workers. Keep synchronous store
functions and per-operation connections, transactions and advisory locks. A
message stream retains its connection until it finishes or is explicitly closed,
including when it is closed before iteration starts.

Worker database calls must finish before cancellation unwinds their owning
connection. A cancelled connection attempt must close any connection it creates.
Use the existing asyncio executor without adding dependencies. Preserve audit
identity propagation and existing durable acceptance/recovery behavior.

Write regressions for event-loop responsiveness during slow connection open,
dependency lookup and close, cancellation during acquisition/queries and stream
cleanup. Run affected database workflows, the provider-free suite, Ruff and
mypy, update the runtime documentation and commit the verified result.

## Outcome

Implemented a shared worker helper in `eki.async_db`, retaining the existing
`eki.graph.workflow.db` export. It propagates acting/audit context and defers
cancellation until the synchronous worker completes, then propagates cancellation
even if the worker also failed. The interview service opens and closes connections
and builds database-backed workflow dependencies through this helper. Connection
acquisition cleans up results created after cancellation; message streams close
their owned connections off-loop, including streams never iterated.

The existing autocommit connections, explicit transactions, session locks,
publication rules and HTTP contracts are retained. No dependency or migration
is needed. SQL statements already running are allowed to finish; cancellation
does not interrupt them at the database server.

Validation on 2026-11-17:

- The initial six responsiveness/cancellation regressions failed against the
  original service and passed after the fix.
- 73 targeted tests passed against disposable Postgres/pgvector schemas,
  covering workflows, delivery, generation, follow-ups, audit and the new
  responsiveness/context tests.
- The complete provider-free suite passed: 897 tests, 11 live/infra deselected.
- An additional real streamed-tool cancellation regression then passed with
  all other async regressions: 10 tests total. It confirms that the tool write
  retains its connection, the receipt becomes retryable, and the saved reply
  recovers without another model call.
- Ruff lint/format, mypy and whitespace checks passed. No provider or real
  source was used. The disposable database was removed after validation.
