# F16: separate frontend workflow and presentation responsibilities

Authorized by the user on 2026-11-17 as review point 4.

Extract interview delivery/receipt recovery into a directly testable controller
and a React hook; render the composer, transcript and topic information as
separate components. Preserve logical message IDs, accepted/confidential draft
clearing, correction isolation, uncertain acceptance, stream locking and recovery.

Split follow-up intake, queue preparation, triage, resolution and proposal review
into feature modules. Split plan listing/review/questions and entry listing,
review/editor/source presentation. Shared entry metadata and maintenance controls
must not depend on route pages. Keep route exports compatible.

Centralize query-key factories and mutation refresh rules, preserving query
scopes and cache behavior. No new dependencies or API/storage changes. Add focused
delivery regressions without requiring a browser, then verify frontend types,
formatting, build, unit/browser tests and real-API browser acceptance. Update the
frontend documentation, rebuild production assets and commit the verified result.

## Result

Implemented the delivery controller and session/delivery hooks, separate interview
components, follow-up intake/queue/proposal modules, plan review modules and entry
review/editor/maintenance modules. Existing route exports delegate to the feature
modules. Query factories and shared refresh policies now cover the workspace, and
TypeScript rejects unused locals and parameters. Production assets were rebuilt.
No dependencies, API contracts or storage schemas changed.

Validation passed: 20 frontend unit tests, 60 Playwright browser tests, the built
UI's real-API/database browser acceptance test, 502 Python tests, TypeScript/Vite
build, Prettier, Ruff lint/format, mypy and `git diff --check`. Delivery tests cover
acceptance and draft clearing, uncertain receipts, correction isolation,
unmounting, stream recovery and authentication failure. Cache tests verify refresh
scope and follow-up comparison invalidation.
