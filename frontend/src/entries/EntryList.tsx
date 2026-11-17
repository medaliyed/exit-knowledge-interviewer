import { queryKeys } from '../queries';
import { useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Link, Navigate, useSearchParams } from 'react-router-dom';
import { useWorkspace } from '../workspace';
import { Badge, Button, Empty, Loading, Notice } from '../ui';
import { Freshness } from './reviewMetadata';
import { type EntrySummary, type PlanSummary, type Session } from '../types';

import { errorMessage } from './messages';
export function Entries() {
  const { api, run, notify } = useWorkspace();
  const cache = useQueryClient();
  const [params, setParams] = useSearchParams();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const plans = useQuery({
    queryKey: queryKeys.plans(run.id),
    queryFn: () => api.request<PlanSummary[]>(`/runs/${run.id}/plans`),
  });
  const conversations = plans.data?.filter(plan => plan.session_id) ?? [];
  const requested = params.get('session');
  const sessionId =
    conversations.find(plan => plan.session_id === requested)?.session_id ??
    conversations[0]?.session_id;
  const entries = useQuery({
    queryKey: queryKeys.entries(sessionId),
    queryFn: () => api.request<EntrySummary[]>(`/sessions/${sessionId}/entries`),
    enabled: Boolean(sessionId),
  });
  const selectedSession = useQuery({
    queryKey: queryKeys.session(sessionId),
    queryFn: () => api.request<Session>(`/sessions/${sessionId}`),
    enabled: Boolean(sessionId),
  });

  async function prepare() {
    if (!sessionId || selectedSession.data?.status !== 'finished') return;
    setBusy(true);
    setError('');
    try {
      await api.request(`/sessions/${sessionId}/synthesise`, 'POST');
      await cache.invalidateQueries({ queryKey: queryKeys.entries(sessionId) });
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
