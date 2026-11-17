import { useQuery } from '@tanstack/react-query';
import { queryKeys } from '../queries';
import { useRef, useState } from 'react';
import { useWorkspace } from '../workspace';
import { Button, Notice } from '../ui';
import { type FollowUpReceipt, type Run } from '../types';

import { describe } from './shared';
export function SaveFollowUp({ question }: { question: string }) {
  const { api, run } = useWorkspace();
  const runs = useQuery({
    queryKey: queryKeys.followUpDestinations(),
    queryFn: () => api.request<Run[]>('/runs'),
  });
  // A global Ask never chooses its recipient implicitly, even when a handover is open.
  const [destination, setDestination] = useState('');
  const [receipt, setReceipt] = useState<FollowUpReceipt | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const ids = useRef(new Map<string, string>());
  const recipient = runs.data?.find(item => item.id === destination);
  async function save() {
    if (!destination || busy) return;
    setBusy(true);
    setError('');
    const key = `${destination}:${question}`;
    const requestId = ids.current.get(key) ?? crypto.randomUUID();
    ids.current.set(key, requestId);
    try {
      setReceipt(
        await api.request<FollowUpReceipt>(`/runs/${destination}/follow-up-requests`, 'POST', {
          request_id: requestId,
          question,
        }),
      );
    } catch (caught) {
      setError(describe(caught));
    } finally {
      setBusy(false);
    }
  }
  async function refresh() {
    if (!receipt) return;
    setBusy(true);
    setError('');
    try {
      setReceipt(await api.request<FollowUpReceipt>(`/follow-up-requests/${receipt.id}`));
    } catch (caught) {
      setError(describe(caught));
    } finally {
      setBusy(false);
    }
  }
  return (
    <section className="stack follow-up-save" aria-label="Save an unanswered question">
      {receipt ? (
        <>
          <p role="status">
            <strong>
              {receipt.status === 'pending'
                ? 'Request saved. It is awaiting owner triage.'
                : receipt.status === 'resolved'
                  ? 'Request resolved.'
                  : 'Request saved.'}
            </strong>
          </p>
          <p className="small">
            Status: {receipt.status.replaceAll('_', ' ')}. This request does not schedule an
            interview or promise an answer.
          </p>
          <Button disabled={busy} onClick={() => void refresh()}>
            Refresh request status
          </Button>
        </>
      ) : (
        <>
          <div className="form-field">
            <label htmlFor="follow-up-destination">Send follow-up request to</label>
            <select
              id="follow-up-destination"
              value={destination}
              disabled={busy || runs.isPending}
              onChange={event => setDestination(event.target.value)}
            >
              <option value="">Choose a handover</option>
              {runs.data?.map(item => (
                <option key={item.id} value={item.id}>
                  {item.expert_name}
                  {item.id === run.id ? ' · currently open' : ''}
                </option>
              ))}
            </select>
          </div>
          {recipient && (
            <p className="small">
              Owner: {recipient.owner_name ?? recipient.expert_name}. They receive your question for
              triage.
            </p>
          )}
          <p className="small muted">
            Save this question only if you want the selected handover’s owner to review it. Your
            question is redacted before storage.
          </p>
          <div>
            <Button disabled={!destination || busy} onClick={() => void save()}>
              {busy ? 'Saving…' : 'Save for a follow-up'}
            </Button>
          </div>
        </>
      )}
      {(error || runs.error) && (
        <Notice error>
          {error || describe(runs.error)}
          {runs.error && <Button onClick={() => void runs.refetch()}>Reload handovers</Button>}
        </Notice>
      )}
    </section>
  );
}
