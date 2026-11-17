import { queryKeys } from './queries';
import { Component, useEffect, useLayoutEffect, useRef, useState, type ReactNode } from 'react';
import {
  BrowserRouter,
  Link,
  Navigate,
  Outlet,
  Route,
  Routes,
  useLocation,
  useNavigate,
  useParams,
} from 'react-router-dom';
import { QueryClient, QueryClientProvider, useQuery } from '@tanstack/react-query';
import { ApiClient, ApiError } from './api';
import type { Identity, Run } from './types';
import { WorkspaceContext } from './workspace';
import { Button, Empty, Loading, Modal, Notice } from './ui';
import { Plans, PlanReview } from './Plans';
import { Interview } from './Interview';
import { Entries, EntryView } from './Entries';
import { Knowledge } from './Knowledge';
import { Maintenance } from './Maintenance';
import { FollowUps, FollowUpProposals } from './FollowUps';
import { AccountMenu, ChangePassword, SetPassword, SignIn, type Message } from './Auth';

function queryClient() {
  return new QueryClient({
    defaultOptions: {
      queries: {
        staleTime: 10_000,
        refetchOnWindowFocus: false,
        retry: (count, error) => count < 1 && !(error instanceof ApiError && error.status < 500),
      },
      mutations: { retry: false },
    },
  });
}
type Auth = { api: ApiClient; identity: Identity; client: QueryClient };
const NOTICE_LIFETIME_MS = 6000;
const period = (value: string) =>
  new Date(`${value.slice(0, 10)}T12:00:00`).toLocaleDateString('en-GB', {
    month: 'short',
    year: 'numeric',
  });
export function handoverPeriod(run: Run) {
  return `${period(run.since)} – ${period(run.until)}`;
}
// Only interview text is worth a warning; search and ask boxes are cheap to retype.
export function hasUnsentAnswer(drafts: Map<string, string>) {
  return [...drafts].some(
    ([key, value]) => /^(answer|correction):/.test(key) && value.trim() !== '',
  );
}

class ErrorBoundary extends Component<{ children: ReactNode }, { error: boolean }> {
  state = { error: false };
  static getDerivedStateFromError() {
    return { error: true };
  }
  render() {
    return this.state.error ? (
      <main className="standalone">
        <Empty title="This page could not open">
          <p>Your saved work is on the server. Refresh, sign in again, and reopen the handover.</p>
          <Button onClick={() => window.location.reload()}>Refresh workspace</Button>
        </Empty>
      </main>
    ) : (
      this.props.children
    );
  }
}

function Shell({
  auth,
  runs,
  notify,
  drafts,
  signOut,
  changePassword,
}: {
  auth: Auth;
  runs: Run[];
  notify: (text: string, error?: boolean) => void;
  drafts: Map<string, string>;
  signOut: () => void;
  changePassword: () => void;
}) {
  const { runId } = useParams();
  const navigate = useNavigate();
  const location = useLocation();
  const run = runs.find(item => item.id === runId);
  const interview = location.pathname.includes('/interview/');
  const content = useRef<HTMLElement>(null);
  useEffect(() => {
    content.current?.focus({ preventScroll: true });
  }, [location.pathname]);
  if (!run) return <Navigate to="/workspace" replace />;
  const base = `/workspace/${run.id}`;
  // An entry page belongs to the review tab for the owner and to approved knowledge for a reader.
  const tab = location.pathname.startsWith(`${base}/follow-ups`)
    ? 'follow-ups'
    : location.pathname.startsWith(`${base}/maintenance`)
      ? 'maintenance'
      : location.pathname.startsWith(`${base}/plans`)
        ? 'plans'
        : location.pathname.startsWith(`${base}/review`) ||
            (location.pathname.startsWith(`${base}/entries/`) && run.can_manage)
          ? 'review'
          : location.pathname.startsWith(`${base}/knowledge`) ||
              location.pathname.startsWith(`${base}/entries/`)
            ? 'knowledge'
            : '';
  return (
    <WorkspaceContext.Provider value={{ ...auth, run, notify, drafts }}>
      <header className="site-header">
        <Link to={base} className="wordmark" aria-label="eki handover home">
          eki
        </Link>
        {interview ? (
          <>
            <span className="header-context">Interview · {run.expert_name}</span>
            <span className="header-spacer" />
            <span className="header-note">
              Submitted answers are saved. Send your draft before leaving.
            </span>
            <Link className="btn" to={`${base}/plans`}>
              Leave for now
            </Link>
            <AccountMenu
              identity={auth.identity}
              signOut={signOut}
              changePassword={changePassword}
            />
          </>
        ) : (
          <>
            <span className="header-spacer" />
            <AccountMenu
              identity={auth.identity}
              signOut={signOut}
              changePassword={changePassword}
            />
          </>
        )}
      </header>
      <div className={interview ? 'interview-shell' : 'workspace-shell'}>
        {!interview && (
          <aside className="handover-sidebar" aria-label="Handovers">
            <h2 className="eyebrow">Handovers</h2>
            <label htmlFor="mobile-handover" className="sr-only">
              Choose handover
            </label>
            <select
              id="mobile-handover"
              className="mobile-handover"
              value={run.id}
              onChange={event => {
                const next = runs.find(item => item.id === event.target.value)!;
                navigateHandover(next);
              }}
            >
              {runs.map(item => (
                <option value={item.id} key={item.id}>
                  {item.expert_name} · {handoverPeriod(item)}
                </option>
              ))}
            </select>
            <nav className="handover-list">
              {runs.map(item => (
                <Link
                  key={item.id}
                  to={`/workspace/${item.id}/${item.can_manage ? (item.maintenance_started_at ? 'maintenance' : 'plans') : 'knowledge'}`}
                  className="handover-link"
                  aria-current={item.id === run.id ? 'page' : undefined}
                >
                  <strong>{item.expert_name}</strong>
                  <span>{handoverPeriod(item)}</span>
                  <span className="handover-role">
                    {item.can_manage ? 'Your handover' : 'Reader'}
                  </span>
                </Link>
              ))}
            </nav>
            <p className="sidebar-note">
              Keep the knowledge.
              <br />
              Honour the source.
            </p>
          </aside>
        )}
        <main
          id="main-content"
          className={interview ? 'interview-page' : 'workspace-main'}
          tabIndex={-1}
          ref={content}
        >
          {!interview && (
            <>
              <div className="handover-heading">
                <p className="eyebrow">Handover</p>
                <h1>
                  {run.expert_name} <span className="handover-period">{handoverPeriod(run)}</span>
                </h1>
                {run.maintenance_started_at && (
                  <p className="small muted">
                    Expert: {run.expert_name} · Maintained by: {run.owner_name ?? 'Unassigned'}
                  </p>
                )}
              </div>
              <nav className="tabs" aria-label="Handover">
                {run.can_manage && (
                  <>
                    <Tab to={`${base}/plans`} active={tab === 'plans'}>
                      Interview plans
                    </Tab>
                    <Tab to={`${base}/follow-ups`} active={tab === 'follow-ups'}>
                      Follow-ups
                    </Tab>
                    <Tab to={`${base}/review`} active={tab === 'review'}>
                      Review entries
                    </Tab>
                    <Tab to={`${base}/maintenance`} active={tab === 'maintenance'}>
                      Maintenance
                    </Tab>
                  </>
                )}
                <Tab to={`${base}/knowledge`} active={tab === 'knowledge'}>
                  Approved knowledge
                </Tab>
              </nav>
            </>
          )}
          <Outlet key={location.pathname} />
        </main>
      </div>
    </WorkspaceContext.Provider>
  );
  function navigateHandover(next: Run) {
    navigate(
      `/workspace/${next.id}/${next.can_manage ? (next.maintenance_started_at ? 'maintenance' : 'plans') : 'knowledge'}`,
    );
  }
}

function Tab({ to, active, children }: { to: string; active: boolean; children: ReactNode }) {
  return (
    <Link
      to={to}
      className={active ? 'active' : undefined}
      aria-current={active ? 'page' : undefined}
    >
      {children}
    </Link>
  );
}
function ManagerOnly({ children, runs }: { children: ReactNode; runs: Run[] }) {
  const { runId } = useParams();
  return runs.find(run => run.id === runId)?.can_manage ? (
    children
  ) : (
    <Navigate to={`/workspace/${runId}/knowledge`} replace />
  );
}
function HandoverIndex({ runs }: { runs: Run[] }) {
  const { runId } = useParams();
  const run = runs.find(item => item.id === runId);
  return (
    <Navigate
      to={run?.can_manage ? (run.maintenance_started_at ? 'maintenance' : 'plans') : 'knowledge'}
      replace
    />
  );
}
function Authenticated({
  auth,
  notify,
  drafts,
  signOut,
  changePassword,
}: {
  auth: Auth;
  notify: (text: string, error?: boolean) => void;
  drafts: Map<string, string>;
  signOut: () => void;
  changePassword: () => void;
}) {
  const runsQuery = useQuery({
    queryKey: queryKeys.runs(auth.identity.subject),
    queryFn: () => auth.api.request<Run[]>('/runs'),
  });
  if (runsQuery.isPending)
    return (
      <main className="standalone">
        <Loading />
      </main>
    );
  if (runsQuery.isError)
    return (
      <main className="standalone">
        <Notice error>Handovers could not load. Your saved work has not changed.</Notice>
        <Button onClick={() => runsQuery.refetch()}>Try again</Button>
        <Button variant="quiet" onClick={signOut}>
          Sign out
        </Button>
      </main>
    );
  const runs = runsQuery.data;
  if (!runs.length)
    return (
      <>
        <header className="site-header">
          <span className="wordmark">eki</span>
          <span className="header-spacer" />
          <AccountMenu identity={auth.identity} signOut={signOut} changePassword={changePassword} />
        </header>
        <main id="main-content" className="standalone">
          <Empty title="No handover is available yet">
            <p>
              None has been shared with you, or your handover is still being prepared. Ask the
              person who invited you.
            </p>
          </Empty>
        </main>
      </>
    );
  const first = runs[0];
  return (
    <Routes>
      <Route
        path="/workspace/:runId"
        element={<Shell {...{ auth, runs, notify, drafts, signOut, changePassword }} />}
      >
        <Route index element={<HandoverIndex runs={runs} />} />
        <Route
          path="plans"
          element={
            <ManagerOnly runs={runs}>
              <Plans />
            </ManagerOnly>
          }
        />
        <Route
          path="plans/:planId"
          element={
            <ManagerOnly runs={runs}>
              <PlanReview />
            </ManagerOnly>
          }
        />
        <Route
          path="interview/:sessionId"
          element={
            <ManagerOnly runs={runs}>
              <Interview />
            </ManagerOnly>
          }
        />
        <Route
          path="maintenance"
          element={
            <ManagerOnly runs={runs}>
              <Maintenance />
            </ManagerOnly>
          }
        />
        <Route
          path="follow-ups"
          element={
            <ManagerOnly runs={runs}>
              <FollowUps />
            </ManagerOnly>
          }
        />
        <Route
          path="follow-ups/:sessionId"
          element={
            <ManagerOnly runs={runs}>
              <FollowUpProposals />
            </ManagerOnly>
          }
        />
        <Route
          path="review"
          element={
            <ManagerOnly runs={runs}>
              <Entries />
            </ManagerOnly>
          }
        />
        <Route path="entries/:entryId" element={<EntryView />} />
        <Route path="knowledge" element={<Knowledge />} />
        <Route
          path="*"
          element={
            <Empty title="This page was not found">
              <Link to={`/workspace/${first.id}`}>Return to your handover</Link>
            </Empty>
          }
        />
      </Route>
      <Route
        path="*"
        element={
          <Navigate
            to={`/workspace/${first.id}/${first.can_manage ? 'plans' : 'knowledge'}`}
            replace
          />
        }
      />
    </Routes>
  );
}

export function App() {
  // Capture the fragment before the layout effect scrubs it, including StrictMode's initial render.
  const [setupToken, setSetupToken] = useState<string | null>(() =>
    window.location.pathname === '/workspace/set-password'
      ? (new URLSearchParams(window.location.hash.slice(1)).get('token') ?? '')
      : null,
  );
  const setupPage = useRef(setupToken !== null);
  const [restoring, setRestoring] = useState(!setupPage.current);
  const [auth, setAuth] = useState<Auth | null>(null);
  const [passwordOpen, setPasswordOpen] = useState(false);
  const [signingOut, setSigningOut] = useState(false);
  useLayoutEffect(() => {
    if (setupPage.current)
      window.history.replaceState(
        window.history.state,
        '',
        window.location.pathname + window.location.search,
      );
  }, []);
  useEffect(() => {
    const captureLink = () => {
      if (window.location.pathname !== '/workspace/set-password' || !window.location.hash) return;
      const token = new URLSearchParams(window.location.hash.slice(1)).get('token') ?? '';
      window.history.replaceState(
        window.history.state,
        '',
        window.location.pathname + window.location.search,
      );
      setSetupToken(token);
      setRestoring(false);
    };
    window.addEventListener('hashchange', captureLink);
    return () => window.removeEventListener('hashchange', captureLink);
  }, []);
  const currentAuth = useRef<Auth | null>(null);
  const previousSubject = useRef('');
  const [message, setMessage] = useState<Message>({ text: '', error: false });
  const drafts = useRef(new Map<string, string>());
  const [signOutOpen, setSignOutOpen] = useState(false);
  const notify = (text: string, error = false) => setMessage({ text, error });
  // Good news clears itself; a problem stays until it is read and dismissed.
  useEffect(() => {
    if (!message.text || message.error) return;
    const timer = setTimeout(() => setMessage({ text: '', error: false }), NOTICE_LIFETIME_MS);
    return () => clearTimeout(timer);
  }, [message]);
  // Drafts live only in this tab, so a refresh or close would lose an unsent answer.
  useEffect(() => {
    const warn = (event: BeforeUnloadEvent) => {
      if (!hasUnsentAnswer(drafts.current)) return;
      event.preventDefault();
      event.returnValue = ''; // older browsers only honour the legacy return value
    };
    window.addEventListener('beforeunload', warn);
    return () => window.removeEventListener('beforeunload', warn);
  }, []);
  const endSession = (expired = false) => {
    const previous = currentAuth.current;
    currentAuth.current = null;
    previous?.api.destroy();
    void previous?.client.cancelQueries();
    previous?.client.clear();
    // An expired identity also invalidates uncertainty metadata in the API client.
    // Keep the route, but never resurrect a possibly accepted private payload.
    drafts.current.clear();
    setAuth(null);
    setPasswordOpen(false);
    setSignOutOpen(false);
    notify(
      expired ? 'Your session has ended. Sign in again to reopen your saved work.' : '',
      expired,
    );
  };
  async function signOut() {
    if (signingOut) return;
    const active = currentAuth.current;
    if (!active) return;
    setSigningOut(true);
    try {
      await active.api.request<void>('/auth/logout', 'POST');
      endSession();
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) endSession();
      else notify('Sign-out could not finish. Try again.', true);
    } finally {
      setSigningOut(false);
    }
  }
  const requestSignOut = () => {
    if (hasUnsentAnswer(drafts.current)) setSignOutOpen(true);
    else void signOut();
  };
  function establish(api: ApiClient, identity: Identity) {
    const previous = currentAuth.current;
    if (previous && previous.api !== api) {
      previous.api.destroy();
      void previous.client.cancelQueries();
      previous.client.clear();
      drafts.current.clear();
      setPasswordOpen(false);
      setSignOutOpen(false);
    }
    if (previousSubject.current !== identity.subject) drafts.current.clear();
    previousSubject.current = identity.subject;
    const next = { api, identity, client: queryClient() };
    currentAuth.current = next;
    setAuth(next);
    setSetupToken(null);
    notify('');
  }
  function sessionClient() {
    const api: ApiClient = new ApiClient(null, () => {
      if (currentAuth.current?.api === api) endSession(true);
    });
    return api;
  }
  async function signIn(email: string, password: string) {
    const api = sessionClient();
    try {
      establish(api, await api.request<Identity>('/auth/login', 'POST', { email, password }));
    } catch (error) {
      api.destroy();
      throw error;
    }
  }
  async function setupPassword(token: string, password: string) {
    const api = sessionClient();
    try {
      establish(
        api,
        await api.request<Identity>('/auth/setup-password', 'POST', {
          token,
          new_password: password,
        }),
      );
    } catch (error) {
      api.destroy();
      throw error;
    }
  }
  useEffect(() => {
    if (setupPage.current) return;
    let mounted = true;
    const api = sessionClient();
    void api
      .request<Identity>('/auth/session')
      .then(identity => {
        if (mounted) establish(api, identity);
      })
      .catch(error => {
        if (mounted && !(error instanceof ApiError && error.status === 401))
          notify('Your session could not be restored. Sign in to reopen your saved work.', true);
      })
      .finally(() => {
        if (mounted) setRestoring(false);
      });
    return () => {
      mounted = false;
      if (currentAuth.current?.api !== api) api.destroy();
    };
  }, []);
  useEffect(
    () => () => {
      currentAuth.current?.api.destroy();
    },
    [],
  );
  return (
    <ErrorBoundary>
      <BrowserRouter>
        <a className="skip-link" href="#main-content">
          Skip to main content
        </a>
        {setupToken !== null ? (
          <SetPassword
            key={setupToken}
            token={setupToken}
            onComplete={setupPassword}
            onReturn={() => setSetupToken(null)}
          />
        ) : restoring ? (
          <main id="main-content" className="standalone">
            <Loading />
          </main>
        ) : auth ? (
          <QueryClientProvider client={auth.client}>
            <Authenticated
              auth={auth}
              notify={notify}
              drafts={drafts.current}
              signOut={requestSignOut}
              changePassword={() => setPasswordOpen(true)}
            />
            <div
              className={`global-notice ${message.error ? 'global-error' : ''}`}
              role={message.error ? 'alert' : 'status'}
              aria-live="polite"
            >
              {message.text && (
                <>
                  <span>{message.text}</span>
                  <button aria-label="Dismiss notice" onClick={() => notify('')}>
                    ×
                  </button>
                </>
              )}
            </div>
            <Modal
              title="Sign out with an unsent answer?"
              open={signOutOpen}
              onClose={() => setSignOutOpen(false)}
            >
              <p>
                You have typed an answer that has not been sent. Signing out discards it. Submitted
                answers are already saved.
              </p>
              <div className="toolbar">
                <Button variant="primary" onClick={() => setSignOutOpen(false)}>
                  Keep writing
                </Button>
                <Button variant="danger" disabled={signingOut} onClick={() => void signOut()}>
                  {signingOut ? 'Signing out…' : 'Sign out and discard'}
                </Button>
              </div>
            </Modal>
            {passwordOpen && (
              <ChangePassword
                api={auth.api}
                onClose={() => setPasswordOpen(false)}
                onSuccess={() => {
                  setPasswordOpen(false);
                  notify('Password changed. Other devices have been signed out.');
                }}
              />
            )}
          </QueryClientProvider>
        ) : (
          <SignIn onSignIn={signIn} message={message} />
        )}
      </BrowserRouter>
    </ErrorBoundary>
  );
}
