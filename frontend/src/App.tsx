import { Component, useEffect, useRef, useState, type FormEvent, type ReactNode } from 'react';
import { BrowserRouter, Link, Navigate, Outlet, Route, Routes, useLocation, useNavigate, useParams } from 'react-router-dom';
import { QueryClient, QueryClientProvider, useQuery } from '@tanstack/react-query';
import { ApiClient, ApiError } from './api';
import type { Identity, Run } from './types';
import { WorkspaceContext } from './workspace';
import { Button, Empty, Loading, Modal, Notice } from './ui';
import { Plans, PlanReview } from './Plans';
import { Interview } from './Interview';
import { Entries, EntryView } from './Entries';
import { Knowledge } from './Knowledge';

function queryClient() {
  return new QueryClient({ defaultOptions: { queries: {
    staleTime: 10_000, refetchOnWindowFocus: false,
    retry: (count, error) => count < 1 && !(error instanceof ApiError && error.status < 500),
  }, mutations: { retry: false } } });
}
type Auth = { api: ApiClient; identity: Identity; client: QueryClient };
type Message = { text: string; error: boolean };
const NOTICE_LIFETIME_MS = 6000;
const period = (value: string) => new Date(`${value.slice(0, 10)}T12:00:00`).toLocaleDateString('en-GB', { month: 'short', year: 'numeric' });
export function handoverPeriod(run: Run) { return `${period(run.since)} – ${period(run.until)}`; }
// Only interview text is worth a warning; search and ask boxes are cheap to retype.
export function hasUnsentAnswer(drafts: Map<string, string>) {
  return [...drafts].some(([key, value]) => /^(answer|correction):/.test(key) && value.trim() !== '');
}

class ErrorBoundary extends Component<{ children: ReactNode }, { error: boolean }> {
  state = { error: false };
  static getDerivedStateFromError() { return { error: true }; }
  render() {
    return this.state.error ? <main className="standalone"><Empty title="This page could not open">
      <p>Your saved work is on the server. Refresh, sign in again, and reopen the handover.</p>
      <Button onClick={() => window.location.reload()}>Refresh workspace</Button>
    </Empty></main> : this.props.children;
  }
}

function SignIn({ onSignIn, message }: { onSignIn: (token: string) => Promise<void>; message: Message }) {
  const [token, setToken] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  async function submit(event: FormEvent) {
    event.preventDefault(); setBusy(true); setError('');
    try { await onSignIn(token.trim()); setToken(''); }
    catch (err) { setError(err instanceof ApiError && err.status === 401 ? 'This access token was not accepted. Check it and try again.'
      : err instanceof ApiError && err.status === 503 ? 'Sign-in is not configured on this server. Ask the person who prepared your handover.'
      : 'The workspace could not be reached. Your token has not been saved. Try again.'); }
    finally { setBusy(false); }
  }
  return <><header className="site-header"><Link to="/" className="wordmark">eki</Link><span className="brand-subtitle">Exit-Knowledge Interviewer</span></header>
    <main id="main-content" className="welcome">
      <section className="welcome-story"><p className="eyebrow">Keep the knowledge. Honour the source.</p>
        <h1>A thoughtful handover.<br />A lasting reference.</h1>
        <ol className="welcome-steps">{[
          ['Make a plan', 'eki reads the selected tickets, code history and emails, and drafts the topics only you know. You decide what stays.'],
          ['Have the conversation', 'One question at a time, over as many sittings as you like. Submitted answers are saved.'],
          ['Make it yours', 'Read each runbook entry, correct it, and approve it. Nothing is published without you.'],
        ].map(([title, detail], index) => <li key={title}><span className="step-number">{index + 1}</span><div><h2>{title}</h2><p>{detail}</p></div></li>)}</ol>
      </section>
      <section className="sheet login-card" id="login" aria-labelledby="signin-title">
        <p className="eyebrow">Your knowledge, in good hands</p><h2 id="signin-title">Sign in</h2>
        <form onSubmit={submit} className="stack">
          <div className="form-field"><label htmlFor="access-token">Access token</label>
            <input id="access-token" type="password" required autoComplete="off" spellCheck={false} value={token}
              onChange={event => setToken(event.target.value)} disabled={busy} aria-describedby="token-help" aria-invalid={!!error} />
            <p className="field-help" id="token-help">The token stays in this tab only. After a refresh you sign in again.</p></div>
          {(error || message.text) && <Notice error={!!error || message.error}>{error || message.text}</Notice>}
          <Button variant="primary" type="submit" disabled={busy}>{busy ? 'Opening workspace…' : 'Open workspace'}<span aria-hidden="true">↗</span></Button>
        </form>
        <p className="login-footnote">A saved conversation is ready whenever you are.</p>
      </section>
    </main><footer className="site-footer">A thoughtful handover. A lasting reference.</footer></>;
}

function Shell({ auth, runs, notify, drafts, signOut }: { auth: Auth; runs: Run[]; notify: (text: string, error?: boolean) => void; drafts: Map<string, string>; signOut: () => void }) {
  const { runId } = useParams();
  const navigate = useNavigate();
  const location = useLocation();
  const run = runs.find(item => item.id === runId);
  const interview = location.pathname.includes('/interview/');
  const content = useRef<HTMLElement>(null);
  useEffect(() => { content.current?.focus({ preventScroll: true }); }, [location.pathname]);
  if (!run) return <Navigate to="/workspace" replace />;
  const base = `/workspace/${run.id}`;
  // An entry page belongs to the review tab for the owner and to approved knowledge for a reader.
  const tab = location.pathname.startsWith(`${base}/plans`) ? 'plans'
    : location.pathname.startsWith(`${base}/review`) || (location.pathname.startsWith(`${base}/entries/`) && run.can_manage) ? 'review'
    : location.pathname.startsWith(`${base}/knowledge`) || location.pathname.startsWith(`${base}/entries/`) ? 'knowledge' : '';
  return <WorkspaceContext.Provider value={{ ...auth, run, notify, drafts }}>
    <header className="site-header"><Link to={base} className="wordmark" aria-label="eki handover home">eki</Link>
      {interview ? <><span className="header-context">Interview · {run.expert_name}</span><span className="header-spacer" />
        <span className="header-note">Submitted answers are saved. Send your draft before leaving.</span><Link className="btn" to={`${base}/plans`}>Leave for now</Link><Button variant="quiet" onClick={signOut}>Sign out</Button></>
        : <><span className="header-spacer" /><span className="identity">Signed in as <strong>{auth.identity.name}</strong></span><Button variant="quiet" onClick={signOut}>Sign out</Button></>}
    </header>
    <div className={interview ? 'interview-shell' : 'workspace-shell'}>
      {!interview && <aside className="handover-sidebar" aria-label="Handovers"><h2 className="eyebrow">Handovers</h2>
        <label htmlFor="mobile-handover" className="sr-only">Choose handover</label><select id="mobile-handover" className="mobile-handover" value={run.id} onChange={event => {
          const next = runs.find(item => item.id === event.target.value)!;
          navigateHandover(next);
        }}>{runs.map(item => <option value={item.id} key={item.id}>{item.expert_name} · {handoverPeriod(item)}</option>)}</select>
        <nav className="handover-list">{runs.map(item => <Link key={item.id} to={`/workspace/${item.id}/${item.can_manage ? 'plans' : 'knowledge'}`} className="handover-link" aria-current={item.id === run.id ? 'page' : undefined}>
          <strong>{item.expert_name}</strong><span>{handoverPeriod(item)}</span><span className="handover-role">{item.can_manage ? 'Your handover' : 'Reader'}</span>
        </Link>)}</nav><p className="sidebar-note">Keep the knowledge.<br />Honour the source.</p></aside>}
      <main id="main-content" className={interview ? 'interview-page' : 'workspace-main'} tabIndex={-1} ref={content}>
        {!interview && <><div className="handover-heading"><p className="eyebrow">Handover</p><h1>{run.expert_name} <span className="handover-period">{handoverPeriod(run)}</span></h1></div>
          <nav className="tabs" aria-label="Handover">{run.can_manage && <><Tab to={`${base}/plans`} active={tab === 'plans'}>Interview plans</Tab><Tab to={`${base}/review`} active={tab === 'review'}>Review entries</Tab></>}
            <Tab to={`${base}/knowledge`} active={tab === 'knowledge'}>Approved knowledge</Tab></nav></>}
        <Outlet key={location.pathname} />
      </main>
    </div>
  </WorkspaceContext.Provider>;
  function navigateHandover(next: Run) { navigate(`/workspace/${next.id}/${next.can_manage ? 'plans' : 'knowledge'}`); }
}

function Tab({ to, active, children }: { to: string; active: boolean; children: ReactNode }) {
  return <Link to={to} className={active ? 'active' : undefined} aria-current={active ? 'page' : undefined}>{children}</Link>;
}
function ManagerOnly({ children, runs }: { children: ReactNode; runs: Run[] }) {
  const { runId } = useParams();
  return runs.find(run => run.id === runId)?.can_manage ? children : <Navigate to={`/workspace/${runId}/knowledge`} replace />;
}
function HandoverIndex({ runs }: { runs: Run[] }) {
  const { runId } = useParams();
  const run = runs.find(item => item.id === runId);
  return <Navigate to={run?.can_manage ? 'plans' : 'knowledge'} replace />;
}
function Authenticated({ auth, notify, drafts, signOut }: { auth: Auth; notify: (text: string, error?: boolean) => void; drafts: Map<string, string>; signOut: () => void }) {
  const runsQuery = useQuery({ queryKey: ['runs', auth.identity.subject], queryFn: () => auth.api.request<Run[]>('/runs') });
  if (runsQuery.isPending) return <main className="standalone"><Loading /></main>;
  if (runsQuery.isError) return <main className="standalone"><Notice error>Handovers could not load. Your saved work has not changed.</Notice><Button onClick={() => runsQuery.refetch()}>Try again</Button><Button variant="quiet" onClick={signOut}>Sign out</Button></main>;
  const runs = runsQuery.data;
  if (!runs.length) return <><header className="site-header"><span className="wordmark">eki</span><span className="header-spacer" /><span>{auth.identity.name}</span><Button variant="quiet" onClick={signOut}>Sign out</Button></header>
    <main id="main-content" className="standalone"><Empty title="No handover is available yet"><p>None has been shared with you, or your handover is still being prepared. Ask the person who gave you your token.</p></Empty></main></>;
  const first = runs[0];
  return <Routes><Route path="/workspace/:runId" element={<Shell {...{ auth, runs, notify, drafts, signOut }} />}>
    <Route index element={<HandoverIndex runs={runs} />} />
    <Route path="plans" element={<ManagerOnly runs={runs}><Plans /></ManagerOnly>} />
    <Route path="plans/:planId" element={<ManagerOnly runs={runs}><PlanReview /></ManagerOnly>} />
    <Route path="interview/:sessionId" element={<ManagerOnly runs={runs}><Interview /></ManagerOnly>} />
    <Route path="review" element={<ManagerOnly runs={runs}><Entries /></ManagerOnly>} />
    <Route path="entries/:entryId" element={<EntryView />} />
    <Route path="knowledge" element={<Knowledge />} />
    <Route path="*" element={<Empty title="This page was not found"><Link to={`/workspace/${first.id}`}>Return to your handover</Link></Empty>} />
  </Route><Route path="*" element={<Navigate to={`/workspace/${first.id}/${first.can_manage ? 'plans' : 'knowledge'}`} replace />} /></Routes>;
}

export function App() {
  const [auth, setAuth] = useState<Auth | null>(null);
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
    const previous = currentAuth.current; currentAuth.current = null;
    previous?.api.destroy(); void previous?.client.cancelQueries(); previous?.client.clear();
    // An expired identity also invalidates uncertainty metadata in the API client.
    // Keep the route, but never resurrect a possibly accepted private payload.
    drafts.current.clear();
    setAuth(null); setSignOutOpen(false); notify(expired ? 'Your token is no longer accepted. Sign in again to reopen your saved work.' : '', expired);
  };
  const requestSignOut = () => { if (hasUnsentAnswer(drafts.current)) setSignOutOpen(true); else endSession(); };
  async function signIn(token: string) {
    let api: ApiClient;
    api = new ApiClient(token, () => { if (currentAuth.current?.api === api) endSession(true); });
    try {
      const identity = await api.request<Identity>('/runbook/identity');
      if (previousSubject.current !== identity.subject) drafts.current.clear();
      previousSubject.current = identity.subject;
      const next = { api, identity, client: queryClient() };
      currentAuth.current = next; setAuth(next); notify('');
    } catch (error) { api.destroy(); throw error; }
  }
  useEffect(() => () => { currentAuth.current?.api.destroy(); }, []);
  return <ErrorBoundary><BrowserRouter><a className="skip-link" href="#main-content">Skip to main content</a>
    {auth ? <QueryClientProvider client={auth.client}><Authenticated auth={auth} notify={notify} drafts={drafts.current} signOut={requestSignOut} />
      <div className={`global-notice ${message.error ? 'global-error' : ''}`} role={message.error ? 'alert' : 'status'} aria-live="polite">{message.text && <><span>{message.text}</span><button aria-label="Dismiss notice" onClick={() => notify('')}>×</button></>}</div>
      <Modal title="Sign out with an unsent answer?" open={signOutOpen} onClose={() => setSignOutOpen(false)}>
        <p>You have typed an answer that has not been sent. Signing out discards it. Submitted answers are already saved.</p>
        <div className="toolbar"><Button variant="primary" onClick={() => setSignOutOpen(false)}>Keep writing</Button><Button variant="danger" onClick={() => endSession()}>Sign out and discard</Button></div>
      </Modal>
    </QueryClientProvider> : <SignIn onSignIn={signIn} message={message} />}
  </BrowserRouter></ErrorBoundary>;
}
