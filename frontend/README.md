# eki workspace

React and TypeScript implement the supplied HTML prototype and `docs/ui/ui-pdr.md`.
Vite builds static files; Tailwind and locally owned shadcn-style Radix components
use the prototype's paper/green design tokens. React Router handles browser
navigation and TanStack Query manages server data. There is no Node production server.

## Develop

Use Node 26.13.0 (the version in `.nvmrc`) and the committed npm lockfile:

```sh
cd frontend
npm ci
npm run dev
```

Run FastAPI separately on `127.0.0.1:8000` using the repository's documented setup.
The Vite server at `http://127.0.0.1:5173` proxies the existing API paths to it.
Sign in with a registered synthetic-workspace token. No provider is called merely
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
reordering, and automated accessibility/layout checks at 360, 768 and 1440 px.
They supplement the Python HTTP/database tests; they do not validate a live model.

To check the committed production build under FastAPI's real Content Security
Policy, start the API and run `EKI_TEST_BASE_URL=http://127.0.0.1:8000 npm run test:e2e`.
The suite rejects policy violations, including when a dialog opens. Optional
`EKI_UI_QA_DIR=/tmp/eki-ui-qa` captures representative synthetic screenshots.

## Data and recovery

Tokens, unsent drafts and caches live in tab memory. A reload requires sign-in.
Signing out clears them and aborts outstanding requests. No persistent browser
storage, analytics, external font, or CDN script is used. Source/model text is
rendered as React text, not interpreted as markup.

`api.ts` owns authenticated JSON/download requests and POST SSE parsing.
`Interview.tsx` preserves Eki's logical message IDs and checks acceptance before
offering a resend. Saved and processed are separate states. Confidential text
is cleared when the server acknowledges acceptance; recovery never asks the
expert to retype an accepted answer. Tests use no real provider credentials.
