import { useEffect, useRef, useState, type FormEvent } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Link, Navigate, useLocation, useParams, useSearchParams } from 'react-router-dom';
import { useWorkspace } from './workspace';
import { Badge, Button, Empty, Loading, Modal, Notice, SectionLabel, Source } from './ui';
import { EntryMaintenance, Freshness, dayAfter, expected } from './Maintenance';
import {
  sections,
  type Citation,
  type Entry,
  type EntrySummary,
  type PlanSummary,
  type SectionName,
  type Session,
  type Turn,
} from './types';

const errorMessage = (error: unknown) =>
  error instanceof Error ? error.message : 'The saved entry could not be loaded. Please try again.';
const date = (value: string) =>
  new Date(value).toLocaleString('en-GB', { dateStyle: 'medium', timeStyle: 'short' });

export function Entries() {
  const { api, run, notify } = useWorkspace();
  const cache = useQueryClient();
  const [params, setParams] = useSearchParams();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const plans = useQuery({
    queryKey: ['plans', run.id],
    queryFn: () => api.request<PlanSummary[]>(`/runs/${run.id}/plans`),
  });
  const conversations = plans.data?.filter(plan => plan.session_id) ?? [];
  const requested = params.get('session');
  const sessionId =
    conversations.find(plan => plan.session_id === requested)?.session_id ??
    conversations[0]?.session_id;
  const entries = useQuery({
    queryKey: ['entries', sessionId],
    queryFn: () => api.request<EntrySummary[]>(`/sessions/${sessionId}/entries`),
    enabled: Boolean(sessionId),
  });
  const selectedSession = useQuery({
    queryKey: ['session', sessionId],
    queryFn: () => api.request<Session>(`/sessions/${sessionId}`),
    enabled: Boolean(sessionId),
  });

  async function prepare() {
    if (!sessionId || selectedSession.data?.status !== 'finished') return;
    setBusy(true);
    setError('');
    try {
      await api.request(`/sessions/${sessionId}/synthesise`, 'POST');
      await cache.invalidateQueries({ queryKey: ['entries', sessionId] });
      notify('Entries prepared from your saved interview. Existing hand edits are kept.');
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setBusy(false);
    }
  }
  if (plans.isPending || (sessionId && entries.isPending)) return <Loading />;
  if (selectedSession.data?.kind === 'follow_up')
    return <Navigate to={`/workspace/${run.id}/follow-ups/${sessionId}`} replace />;
  const approved = entries.data?.filter(entry => entry.status === 'approved').length ?? 0;
  const drafts = entries.data?.filter(entry => entry.status === 'draft').length ?? 0;
  const rejected = entries.data?.filter(entry => entry.status === 'rejected').length ?? 0;
  return (
    <div className="stack page-content">
      <div className="page-heading">
        <div>
          <p className="eyebrow">Your knowledge, in your words</p>
          <h1>Review entries</h1>
          <p className="page-intro">
            Read each runbook entry, fix the wording, then approve or reject it. Colleagues see only
            what you approve.
          </p>
        </div>
      </div>
      {(plans.error || entries.error || error) && (
        <Notice error>
          {error || errorMessage(plans.error ?? entries.error)}{' '}
          <Button
            variant="quiet"
            onClick={() => {
              void plans.refetch();
              if (sessionId) void entries.refetch();
            }}
          >
            Try again
          </Button>
        </Notice>
      )}
      {conversations.length > 1 && (
        <div className="form-field interview-picker">
          <label htmlFor="review-conversation">Conversation</label>
          <select
            id="review-conversation"
            value={sessionId ?? ''}
            onChange={event => setParams({ session: event.target.value })}
          >
            {conversations.map(plan => (
              <option value={plan.session_id!} key={plan.id}>
                Interview for plan{' '}
                {(plans.data?.length ?? 0) -
                  (plans.data?.findIndex(item => item.id === plan.id) ?? 0)}
              </option>
            ))}
          </select>
        </div>
      )}
      {entries.data?.length ? (
        <>
          <p className="muted">
            {approved} of {entries.data.length} approved. {drafts}{' '}
            {drafts === 1 ? 'draft waiting' : 'drafts waiting'} for you
            {rejected ? `, ${rejected} rejected` : ''}.
          </p>
          <div className="entry-list">
            {entries.data.map(entry => (
              <article className="list-row entry-row" key={entry.id}>
                <div className="grow">
                  <h2 className="card-title">
                    <Link to={`/workspace/${run.id}/entries/${entry.id}`}>{entry.title}</Link>
                  </h2>
                  <div className="inline small muted">
                    {entry.edited && <Badge>Edited by hand</Badge>}
                    {Boolean(entry.unsupported_count) && (
                      <span className="warning-text">
                        {entry.unsupported_count}{' '}
                        {entry.unsupported_count === 1 ? 'claim' : 'claims'} to check
                      </span>
                    )}
                    <span>v{entry.version}</span>
                    <Freshness value={entry} />
                    {entry.open_question_count ? (
                      <span>{entry.open_question_count} left open</span>
                    ) : null}
                  </div>
                </div>
                <Badge status={entry.status}>{entry.status}</Badge>
                <Link className="btn" to={`/workspace/${run.id}/entries/${entry.id}`}>
                  {entry.status === 'draft' ? 'Review' : 'Open'} <span aria-hidden="true">→</span>
                </Link>
              </article>
            ))}
          </div>
        </>
      ) : (
        !(plans.error || entries.error) && (
          <Empty title="Nothing to review yet">
            <p>Entries appear when the saved interview is ready to become a runbook.</p>
            {sessionId ? (
              <div className="toolbar">
                <Link className="btn" to={`/workspace/${run.id}/interview/${sessionId}`}>
                  Open interview
                </Link>
                {selectedSession.data?.status === 'finished' && (
                  <Button disabled={busy} onClick={() => void prepare()}>
                    {busy ? 'Preparing entries…' : 'Prepare entries from saved interview'}
                  </Button>
                )}
              </div>
            ) : (
              <Link className="btn" to={`/workspace/${run.id}/plans`}>
                Go to interview plans
              </Link>
            )}
          </Empty>
        )
      )}
    </div>
  );
}

export function EntryView() {
  const { entryId = '' } = useParams();
  const location = useLocation();
  const { api, run, identity, notify } = useWorkspace();
  const cache = useQueryClient();
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [reviewBy, setReviewBy] = useState(dayAfter(90));
  const [rejectOpen, setRejectOpen] = useState(false);
  const [reason, setReason] = useState('');
  const [reasonError, setReasonError] = useState('');
  const [editing, setEditing] = useState<SectionName | null>(null);
  const [claims, setClaims] = useState<string[]>([]);
  const [openQuestion, setOpenQuestion] = useState('');
  const [editorError, setEditorError] = useState('');
  const [sourceOpen, setSourceOpen] = useState(false);
  const [sourceLoading, setSourceLoading] = useState(false);
  const [sourceText, setSourceText] = useState('');
  const [sourceLabel, setSourceLabel] = useState('');
  const [sourceError, setSourceError] = useState('');
  const sourceRequest = useRef(0);
  const currentEntry = useRef(entryId);
  currentEntry.current = entryId;
  useEffect(() => {
    sourceRequest.current += 1;
    setError('');
    setBusy(false);
    setHistoryOpen(false);
    setRejectOpen(false);
    setReason('');
    setReasonError('');
    setEditing(null);
    setClaims([]);
    setOpenQuestion('');
    setEditorError('');
    setSourceOpen(false);
    setSourceText('');
    setSourceError('');
  }, [entryId]);
  const entryQuery = useQuery({
    queryKey: ['entry', entryId],
    queryFn: () => api.request<Entry>(`/entries/${entryId}`),
  });
  const entry = entryQuery.data;
  useEffect(() => {
    if (
      !entry ||
      !/^#section-(trigger|diagnosis|steps|rollback|contacts|pitfalls|rationale)$/.test(
        location.hash,
      )
    )
      return;
    const section = document.getElementById(location.hash.slice(1));
    section?.scrollIntoView({ block: 'start' });
    section?.focus({ preventScroll: true });
  }, [entry?.id, location.hash]);
  // Reader responses omit management fields, even when opened from cross-handover search.
  const manager = Boolean(run.can_manage && entry?.decisions);
  const canReview = manager && entry?.can_review !== false;

  function editSection(name: SectionName) {
    if (!entry || !canReview) return;
    setEditing(name);
    setClaims(entry[name].claims.map(claim => claim.text));
    setOpenQuestion(entry[name].open_question ?? '');
    setEditorError('');
  }
  async function update(path: string, method: string, body: object, success: string) {
    setBusy(true);
    setError('');
    try {
      const updated = await api.request<Entry>(path, method, {
        ...(entry ? expected(entry) : {}),
        ...body,
      });
      cache.setQueryData(['entry', entryId], updated);
      await Promise.all([
        cache.invalidateQueries({ queryKey: ['entries'] }),
        cache.invalidateQueries({ queryKey: ['approved-entries'] }),
        cache.invalidateQueries({ queryKey: ['session'] }),
        cache.invalidateQueries({ queryKey: ['follow-up-targets'] }),
        cache.invalidateQueries({ queryKey: ['follow-up-gaps'] }),
        cache.invalidateQueries({ queryKey: ['follow-up-proposals'] }),
      ]);
      if (currentEntry.current === entryId) notify(success);
      return true;
    } catch (caught) {
      if (currentEntry.current === entryId) setError(errorMessage(caught));
      return false;
    } finally {
      if (currentEntry.current === entryId) setBusy(false);
    }
  }
  async function saveSection(event: FormEvent) {
    event.preventDefault();
    if (!entry || !editing) return;
    const statements = claims.map(claim => claim.trim()).filter(Boolean);
    if (!statements.length && !openQuestion.trim()) {
      setEditorError('Keep at least one statement or an open question for this section.');
      return;
    }
    const ok = await update(
      `/entries/${entry.id}`,
      'PATCH',
      {
        edited_by: identity.name,
        sections: { [editing]: { claims: statements, open_question: openQuestion.trim() || null } },
      },
      'Edits saved with your attribution. The entry is a draft until you approve it.',
    );
    if (ok) setEditing(null);
  }
  async function approve() {
    if (!entry) return;
    if (!reviewBy || reviewBy < dayAfter(1) || reviewBy > dayAfter(365)) {
      setError('Choose a review date from tomorrow through 365 days ahead.');
      return;
    }
    const missing = sections.filter(
      name => !entry[name].claims.length && !entry[name].open_question,
    );
    const uncited = sections.filter(name =>
      entry[name].claims.some(claim => claim.citations.length === 0),
    );
    if (missing.length || uncited.length) {
      setError(
        [
          missing.length ? `Add a statement or open question for: ${missing.join(', ')}.` : '',
          uncited.length ? `Every statement needs a source. Check: ${uncited.join(', ')}.` : '',
        ]
          .filter(Boolean)
          .join(' '),
      );
      return;
    }
    await update(
      `/entries/${entry.id}/approve`,
      'POST',
      { approved_by: identity.name, review_by: reviewBy },
      'Entry approved. It is now visible to permitted readers.',
    );
  }
  async function reject(event: FormEvent) {
    event.preventDefault();
    if (!entry || !reason.trim()) {
      setReasonError('Add a short reason so the decision makes sense later.');
      return;
    }
    if (
      await update(
        `/entries/${entry.id}/reject`,
        'POST',
        { rejected_by: identity.name, reason: reason.trim() },
        'Entry rejected. It is kept in review history and excluded from reader access.',
      )
    ) {
      setRejectOpen(false);
      setReason('');
    }
  }
  async function openSource(citation: Citation) {
    if (!manager || citation.kind !== 'interview_turn') return;
    const requestId = ++sourceRequest.current;
    setSourceOpen(true);
    setSourceLoading(true);
    setSourceError('');
    setSourceText('');
    setSourceLabel(`${citation.author_name ?? 'Expert answer'} · turn ${citation.turn_index}`);
    try {
      const session = await api.request<Session>(`/sessions/${citation.session_id}`);
      const turn = session.turns.find((item: Turn) => item.ordinal === citation.turn_index);
      if (sourceRequest.current === requestId)
        setSourceText(
          turn
            ? turn.confidential
              ? '[confidential]'
              : turn.text
            : 'This source turn is unavailable.',
        );
    } catch (caught) {
      if (sourceRequest.current === requestId) setSourceError(errorMessage(caught));
    } finally {
      if (sourceRequest.current === requestId) setSourceLoading(false);
    }
  }
  async function download() {
    setBusy(true);
    setError('');
    try {
      await api.download(`/entries/${entryId}/markdown`, `eki-entry-${entryId}.md`);
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setBusy(false);
    }
  }

  if (entryQuery.isPending) return <Loading />;
  if (!entry)
    return (
      <Notice error>
        {errorMessage(entryQuery.error)}{' '}
        <Button onClick={() => void entryQuery.refetch()}>Try again</Button>
      </Notice>
    );
  return (
    <article className="page-content entry-detail stack">
      <Link
        className="back-link"
        to={
          manager
            ? `/workspace/${run.id}/review${entry.session_id ? `?session=${entry.session_id}` : ''}`
            : `/workspace/${run.id}/knowledge`
        }
      >
        ← {manager ? 'All entries' : 'Search and ask'}
      </Link>
      <div className="page-heading">
        <div>
          <div className="inline">
            <Badge status={entry.status}>{entry.status}</Badge>
            {Boolean(entry.unsupported?.length) && (
              <Badge status="rejected">
                {entry.unsupported?.length} {entry.unsupported?.length === 1 ? 'claim' : 'claims'}{' '}
                to check
              </Badge>
            )}
            <span className="small muted">v{entry.version}</span>
          </div>
          <h1>{entry.title}</h1>
          <p className="page-intro">
            {manager
              ? 'Every statement cites the answer it came from. Select a source to read that answer.'
              : 'Approved statements and their sources. The private interview is not shared with readers.'}
          </p>
          {entry.approved_by && entry.approved_at && (
            <p className="small muted">
              Approved by {entry.approved_by} · {date(entry.approved_at)} · version {entry.version}
            </p>
          )}
        </div>
        <div className="toolbar">
          {manager && (
            <Button variant="quiet" onClick={() => setHistoryOpen(true)}>
              History
            </Button>
          )}
          {entry.status === 'approved' && (
            <Button disabled={busy} onClick={() => void download()}>
              Download as Markdown
            </Button>
          )}
        </div>
      </div>
      {manager && !canReview && (
        <Notice>
          {entry.maintenance_started_at
            ? `The current maintainer, ${entry.owner_name ?? 'the run owner'}, reviews this entry. Your account has inspection access.`
            : 'Capture review requires the original expert identity.'}
        </Notice>
      )}
      <EntryMaintenance
        entry={entry}
        manager={manager}
        canReview={canReview}
        onChanged={() =>
          Promise.all([
            entryQuery.refetch(),
            cache.invalidateQueries({ queryKey: ['maintenance'] }),
            cache.invalidateQueries({ queryKey: ['entries'] }),
            cache.invalidateQueries({ queryKey: ['approved-entries'] }),
          ])
        }
      />
      {error && <Notice error>{error}</Notice>}
      {entry.rejection_reason && (
        <Notice error>
          Rejected: {entry.rejection_reason}. Edit the entry to return it to draft for review.
        </Notice>
      )}
      {entry.summary && <p>{entry.summary}</p>}
      <div className="entry-sections">
        {sections.map(name => (
          <section className="section" key={name} id={`section-${name}`} tabIndex={-1}>
            <div className="section-heading">
              <h2>
                <SectionLabel name={name} />
              </h2>
              {canReview && (
                <Button
                  variant="quiet"
                  disabled={busy}
                  aria-label={`Edit ${name}`}
                  onClick={() => editSection(name)}
                >
                  Edit
                </Button>
              )}
            </div>
            <div className="section-content">
              {entry[name].claims.length > 0 &&
                (name === 'steps' ? (
                  <ol className="steps">
                    {entry[name].claims.map((claim, index) => (
                      <li key={index}>
                        <ClaimContent
                          text={claim.text}
                          citations={claim.citations}
                          unsupported={
                            entry.unsupported?.find(
                              flag => flag.section === name && flag.claim_index === index,
                            )?.terms
                          }
                          manager={manager}
                          openSource={citation => void openSource(citation)}
                        />
                      </li>
                    ))}
                  </ol>
                ) : (
                  <div className="stack">
                    {entry[name].claims.map((claim, index) => (
                      <div key={index}>
                        <ClaimContent
                          text={claim.text}
                          citations={claim.citations}
                          unsupported={
                            entry.unsupported?.find(
                              flag => flag.section === name && flag.claim_index === index,
                            )?.terms
                          }
                          manager={manager}
                          openSource={citation => void openSource(citation)}
                        />
                      </div>
                    ))}
                  </div>
                ))}
              {entry[name].open_question && (
                <p className="gap-state">
                  <span aria-hidden="true" className="status-mark status-open" />
                  <span>
                    {entry[name].open_question.toLowerCase().includes('confidential') ? (
                      entry[name].open_question
                    ) : (
                      <>
                        <strong>Open:</strong> {entry[name].open_question}
                      </>
                    )}
                  </span>
                </p>
              )}
              {!entry[name].claims.length && !entry[name].open_question && (
                <p className="gap-state">Not answered</p>
              )}
            </div>
          </section>
        ))}
      </div>
      {canReview && (
        <div className="page-actions">
          <div>
            <p className="muted">
              {entry.status === 'draft'
                ? 'Approving publishes this entry to permitted colleagues. Open and confidential sections stay visible as labelled gaps.'
                : entry.status === 'approved'
                  ? 'Editing returns this entry to draft and removes it from reader access until you approve it again.'
                  : 'This rejected entry is excluded from reader access. Edit a section to reopen it as a draft.'}
            </p>
            {!entry.maintenance_started_at &&
              identity.name.trim() !== run.expert_name.trim() &&
              entry.status === 'draft' && (
                <p className="small warning-text">
                  Only {run.expert_name} can approve. Your signed-in name is {identity.name}.
                </p>
              )}
          </div>
          {entry.status === 'draft' && (
            <div className="stack">
              <div className="form-field">
                <label htmlFor="approval-review-by">Next review by (UTC)</label>
                <input
                  id="approval-review-by"
                  type="date"
                  required
                  min={dayAfter(1)}
                  max={dayAfter(365)}
                  value={reviewBy}
                  onChange={event => setReviewBy(event.target.value)}
                />
              </div>
              <div className="toolbar">
                <Button
                  variant="danger"
                  disabled={busy}
                  onClick={() => {
                    setRejectOpen(true);
                    setReasonError('');
                  }}
                >
                  Reject
                </Button>
                <Button variant="primary" disabled={busy} onClick={() => void approve()}>
                  {busy ? 'Saving…' : 'Approve'}
                </Button>
              </div>
            </div>
          )}
        </div>
      )}
      <Modal title="History" open={historyOpen} onClose={() => setHistoryOpen(false)}>
        <p className="small muted">
          Current version: {entry.version}. Every decision is recorded below.
        </p>
        {entry.decisions?.length ? (
          <ol className="decision-list">
            {entry.decisions.map((decision, index) => (
              <li key={index}>
                <strong>
                  {decision.action === 'approve'
                    ? 'Approved'
                    : decision.action === 'reject'
                      ? 'Rejected'
                      : decision.action === 'edit'
                        ? 'Edited by hand'
                        : decision.action === 'follow_up_apply'
                          ? 'Follow-up sections applied'
                          : decision.action === 'review'
                            ? 'Reviewed; no changes'
                            : decision.action === 'withdraw'
                              ? 'Withdrawn to draft'
                              : 'Source corrected'}
                </strong>
                <p className="small muted">
                  {decision.who} · {date(decision.at)}
                </p>
                {decision.reason && <p>{decision.reason}</p>}
              </li>
            ))}
          </ol>
        ) : (
          <p>No decisions have been recorded yet.</p>
        )}
      </Modal>
      <Modal
        title="Reject this entry?"
        open={rejectOpen}
        onClose={() => {
          if (!busy) setRejectOpen(false);
        }}
      >
        <form className="stack" onSubmit={event => void reject(event)}>
          <p>
            A rejected entry is excluded from reader access. Your saved answers and this decision
            stay in the review history.
          </p>
          <div className="form-field">
            <label htmlFor="rejection-reason">Reason</label>
            <textarea
              id="rejection-reason"
              value={reason}
              disabled={busy}
              onChange={event => {
                setReason(event.target.value);
                setReasonError('');
              }}
              aria-invalid={Boolean(reasonError)}
              aria-describedby="rejection-help"
            />
            <p className={`field-help ${reasonError ? 'error-text' : ''}`} id="rejection-help">
              {reasonError || 'Add a short reason so the decision makes sense later.'}
            </p>
          </div>
          {error && <Notice error>{error}</Notice>}
          <div className="toolbar">
            <Button disabled={busy} onClick={() => setRejectOpen(false)}>
              Cancel
            </Button>
            <Button variant="danger" type="submit" disabled={busy}>
              {busy ? 'Saving decision…' : 'Reject entry'}
            </Button>
          </div>
        </form>
      </Modal>
      <Modal
        title={`Edit ${editing ?? 'section'}`}
        open={editing !== null}
        onClose={() => {
          if (!busy) setEditing(null);
        }}
      >
        <form className="stack section-editor" onSubmit={event => void saveSection(event)}>
          {entry.status === 'approved' && (
            <Notice error>
              Saving removes this approved entry from reader access until you approve the updated
              draft.
            </Notice>
          )}
          <p className="small muted">
            Write each statement in your own words. Your edit is saved as a new interview turn,
            which becomes its source.
          </p>
          <ol className="editor-claims stack">
            {claims.map((claim, index) => (
              <li key={index}>
                <div className="form-field">
                  <label htmlFor={`edit-claim-${index}`}>
                    {editing === 'steps' ? 'Step' : 'Statement'} {index + 1}
                  </label>
                  <div className="inline editor-claim">
                    <textarea
                      id={`edit-claim-${index}`}
                      value={claim}
                      disabled={busy}
                      onChange={event =>
                        setClaims(previous =>
                          previous.map((value, at) => (at === index ? event.target.value : value)),
                        )
                      }
                    />
                    <Button
                      variant="quiet"
                      disabled={busy}
                      aria-label={`Remove ${editing === 'steps' ? 'step' : 'statement'} ${index + 1}`}
                      onClick={() =>
                        setClaims(previous => previous.filter((_, at) => at !== index))
                      }
                    >
                      ×
                    </Button>
                  </div>
                </div>
              </li>
            ))}
          </ol>
          <div>
            <Button
              variant="quiet"
              disabled={busy}
              onClick={() => setClaims(previous => [...previous, ''])}
            >
              Add {editing === 'steps' ? 'a step' : 'a statement'}
            </Button>
          </div>
          <div className="form-field">
            <label htmlFor="edit-open-question">Open question or remaining gap</label>
            <textarea
              id="edit-open-question"
              value={openQuestion}
              disabled={busy}
              onChange={event => setOpenQuestion(event.target.value)}
              aria-describedby="edit-open-help"
            />
            <p className="field-help" id="edit-open-help">
              Keep an honest gap when the answer is unknown. Every section needs at least a
              statement or an open question.
            </p>
          </div>
          {editorError && <Notice error>{editorError}</Notice>}
          {error && <Notice error>{error}</Notice>}
          <div className="toolbar">
            <Button variant="primary" type="submit" disabled={busy}>
              {busy ? 'Saving…' : 'Save section'}
            </Button>
            <Button disabled={busy} onClick={() => setEditing(null)}>
              Cancel
            </Button>
          </div>
        </form>
      </Modal>
      <Modal
        title={sourceLabel || 'Source answer'}
        open={sourceOpen}
        onClose={() => setSourceOpen(false)}
      >
        {sourceLoading ? (
          <Loading />
        ) : sourceError ? (
          <Notice error>{sourceError}</Notice>
        ) : (
          <p className="source-panel preserve-lines">{sourceText}</p>
        )}
      </Modal>
    </article>
  );
}

function ClaimContent({
  text,
  citations,
  unsupported,
  manager,
  openSource,
}: {
  text: string;
  citations: Citation[];
  unsupported?: string[];
  manager: boolean;
  openSource: (citation: Citation) => void;
}) {
  return (
    <>
      <p className="claim preserve-lines">{text}</p>
      <div className="source-list">
        {citations.map((citation, index) => (
          <span key={index}>
            <Source
              citation={citation}
              onClick={
                manager && citation.kind === 'interview_turn'
                  ? () => openSource(citation)
                  : undefined
              }
            />
            {citation.kind === 'artifact' && citation.quote && (
              <q className="small muted">{citation.quote}</q>
            )}
          </span>
        ))}
        {!citations.length && (
          <span className="warning-text small">This statement needs a source before approval.</span>
        )}
      </div>
      {unsupported?.length ? (
        <p className="hint">
          Not in the cited answer: {unsupported.join(', ')}. Check the wording against the cited
          answer.
        </p>
      ) : null}
    </>
  );
}
