# F6/F7: require the reviewed plan revision

Authorized by the user on 2026-11-17 as review point 1.

Every original or follow-up plan edit and approval must name the revision the
caller reviewed. Increment revisions on every saved update and compare them
under the plan row lock, including the final save during approval. Reject stale
requests with 409 before changing the plan or starting the interview. HTTP
requests missing a positive revision fail validation. Browser and CLI callers
send the displayed revision; browser conflicts refresh the plan and require a
new explicit action. Preserve existing follow-up approval recovery.

Validate with stale edit/approval, missing revision and store race regression
tests, browser request/recovery tests, the affected database workflows, and the
existing Python/frontend checks. Rebuild committed browser assets and update
the plan flow/API documentation. No schema change or provider call is needed.

## Outcome

Implemented mandatory revisions in HTTP bodies and shared service calls, with
updated browser, CLI and grader callers. Original plans now advance revisions
and receive the same locked save check as follow-ups. Browser conflicts close
stale confirmations, refresh the plan and require a new explicit submission.
Production browser assets have been rebuilt. Existing revision columns suffice;
no migration is required. External HTTP clients must add `expected_revision`.

Validation on 2026-11-17:

- 888 Python tests passed, 11 live/infra tests deselected, using a disposable
  Postgres/pgvector container. This includes the real-browser/API acceptance
  journey, stale original edits/approvals, missing/nonpositive revisions and
  an edit committed between the approval revision check and final save.
- All 58 Playwright browser tests passed, including conflict refresh and
  explicit retry for both original-plan edits and approvals.
- All 11 frontend unit tests passed; the production build, Ruff lint/format,
  mypy, TypeScript, Prettier and whitespace checks passed.
- No live provider or source was used. The disposable test database was removed
  after validation.
