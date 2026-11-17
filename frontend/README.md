# eki workspace

React and TypeScript implement the supplied HTML prototype and `docs/ui/ui-pdr.md`.
Vite builds static files; Tailwind and locally owned shadcn-style Radix components
use the prototype's paper/green design tokens. React Router handles browser
navigation and TanStack Query manages server data. There is no Node production server.

## Source organization

Route exports remain in `Interview.tsx`, `FollowUps.tsx`, `Plans.tsx` and
`Entries.tsx`. Feature modules separate rendering from workflow state:

| Location | Responsibility |
| --- | --- |
| `interview/` | Delivery/receipt recovery controller, React integration, session queries, composer, transcript and topic components |
| `followups/` | Reader intake, queue preparation, triage, resolution and proposal review |
| `plans/` | Listing, review workflow, editable questions and error messages |
| `entries/` | Listing, review workflow, editing, history, claims, shared freshness metadata and entry maintenance controls |
| `queries.ts` | Query-key factories and mutation refresh policies |

Use the shared query factories for new queries and extend the relevant refresh
policy when a mutation affects another screen. Keep recovery decisions in the
delivery controller so they can be tested without rendering React. TypeScript
rejects unused locals and parameters to keep feature boundaries clear.

## Develop

Use Node 26.13.0 (the version in `.nvmrc`) and the committed npm lockfile:

```sh
cd frontend
npm ci
npm run dev
```

Run FastAPI separately on `127.0.0.1:8000` using the repository's documented setup.
The Vite server at `http://127.0.0.1:5173` proxies the existing API paths to it.
Set `EKI_PUBLIC_ORIGIN=http://127.0.0.1:5173` on the API for Vite development
and `EKI_SESSION_COOKIE_SECURE=false` only for local HTTP development. The proxy
preserves the browser Origin so the backend can verify it. Create a synthetic
account with `eki users add`, open its invitation link on the Vite origin, and
sign in with email and password. No provider is called merely
by opening the UI; drafting and interviewing use the backend's configured model.

## Build and verify

```sh
npm run build
npm test
npm run test:e2e
```

The build writes `src/eki/web/static/workspace/`. Keep its generated HTML, JS and
CSS in the same change as the frontend source so a Python installation serves
the UI immediately. FastAPI serves it at `/` and `/workspace/*`, and serves its
assets under `/assets/workspace/`. API paths remain separate from browser paths.

Browser tests use a synthetic stateful API double and the installed Google
Chrome on a development machine. In CI, install Playwright Chromium first:
`npx playwright install --with-deps chromium`. Tests cover routing, confidential
answers, interruption recovery, explicit publication, search/export, keyboard
reordering, unsent-answer warnings, confirmations before destructive actions,
and automated accessibility/layout checks at 360, 768 and 1440 px.
They supplement the Python HTTP/database tests; they do not validate a live model.

The database-backed browser acceptance test runs the compiled UI against a real
FastAPI server and disposable Postgres schema, with scripted model doubles. From
the repository root, after installing the frontend dependencies and browser:

```sh
.venv/bin/pytest -q -m db tests/test_follow_up_browser.py
```

It uses installed Google Chrome locally and Playwright Chromium in CI. CI runs
this path explicitly in addition to the mocked browser suite. It covers reader
request intake, owner selection, a focused interview, proposal apply, separate
approval and gap resolution without a provider credential or paid model call.

To check the committed production build under FastAPI's real Content Security
Policy, start the API and run `EKI_TEST_BASE_URL=http://127.0.0.1:8000 npm run test:e2e`.
The suite rejects policy violations, including when a dialog opens. Optional
`EKI_UI_QA_DIR=/tmp/eki-ui-qa` captures representative synthetic screenshots.

## Data and recovery

The server keeps sessions behind an HttpOnly cookie; a reload restores sign-in
and the current route until the session expires. Unsent drafts and caches live
only in tab memory, so reloading still discards unsent text after a leave-page
warning. Signing out revokes the server session, clears drafts and caches, and
aborts outstanding requests. Invitation/reset tokens are read from the URL
fragment and immediately removed from the address bar. The account menu offers
password changes, which keep this device signed in and revoke other sessions. No persistent browser
storage, analytics, external font, or CDN script is used. Source/model text is
rendered as React text, not interpreted as markup.

`api.ts` owns authenticated JSON/download requests and POST SSE parsing.
`interview/delivery.ts` preserves Eki's logical message IDs and checks acceptance
before offering a resend; `useInterviewDelivery.ts` connects it to React. Saved
and processed are separate states. Confidential text
is cleared when the server acknowledges acceptance; recovery never asks the
expert to retype an accepted answer. Tests use no real provider credentials.

## Follow-up workflow

Owners have a **Follow-ups** tab at `/workspace/:runId/follow-ups`. The queue
combines eligible public open sections and explicitly saved reader questions.
It supports mapping, editing seed questions, priority, grouping, deferral,
dismissal and reopening. Questions are selected for a finished original
interview, with visible topic, question, model and embedding limits. Preparation
does not call a model. Only the available original expert, authenticated as the
current owner, can approve and answer a follow-up.

The plan screen identifies selected sections, allows seed edits/removal and
discards unused draft plans with their gap reservations. The interview reuses
normal message receipts, confidential answers, corrections and recovery. It
shows selected sections and durable usage; **Stop for now** retains accepted
work and returns unanswered questions to triage. The composer stays locked
between acceptance and completion of the streamed reply.

Finished follow-ups open `/workspace/:runId/follow-ups/:sessionId`. Each private
proposal compares existing statements with additions and lets the reviewer
expand source answers, select sections, and merge or explicitly replace them.
A stale comparison requires explicit refresh and review. Apply returns the
original entry to draft; ordinary entry approval publishes it. New topics have
their own draft entries on the same completion page. Filled public-section
gaps resolve on approval; saved reader questions require separate confirmation
against a specific approved entry version.

An unsupported Ask result offers **Save for a follow-up** only after the reader
explicitly chooses the destination handover and sees its owner. The saved
receipt can be refreshed. Evidence changes, context limits, review-required
results and transport errors have distinct recovery guidance. Readers never
receive the owner queue, proposals or private transcript access. The follow-up
browser suite checks these boundaries, proposal conflicts, delayed reply
locking, partial completion, explicit resolution, keyboard operation and narrow
layout/accessibility.
