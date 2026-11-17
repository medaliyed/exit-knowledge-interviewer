import { refreshAfterHandover, queryKeys } from './queries';
import { useState, type FormEvent } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { useWorkspace } from './workspace';
import { Button, Empty, Loading, Modal, Notice } from './ui';
import { type EntrySummary, type MaintenanceState } from './types';

const message = (error: unknown) =>
  error instanceof Error
    ? error.message
    : 'The maintenance change could not be saved. Refresh and try again.';
export { dayAfter, expected, Freshness } from './entries/reviewMetadata';
export { EntryMaintenance } from './entries/EntryMaintenance';
import { Freshness } from './entries/reviewMetadata';
export function Maintenance() {
  const { api, run, identity, notify } = useWorkspace();
  const cache = useQueryClient();
  const [filter, setFilter] = useState('all');
  const [offset, setOffset] = useState(0);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [transferOpen, setTransferOpen] = useState(false);
  const [subject, setSubject] = useState('');
  const [reason, setReason] = useState('');
  const state = useQuery({
    queryKey: queryKeys.maintenanceState(run.id),
    queryFn: () => api.request<MaintenanceState>(`/runs/${run.id}/maintenance`),
  });
  const queue = useQuery({
    queryKey: queryKeys.maintenance(run.id, filter, offset),
    queryFn: () =>
      api.request<{ entries: EntrySummary[]; next_offset: number | null }>(
        `/runs/${run.id}/maintenance/entries?filter=${filter}&offset=${offset}`,
      ),
  });
  async function transfer(event: FormEvent) {
    event.preventDefault();
    if (!state.data || !subject.trim() || !reason.trim() || busy) return;
    setBusy(true);
    setError('');
    try {
      await api.request(`/runs/${run.id}/handover`, 'POST', {
        owner_subject: subject.trim(),
        expected_owner_subject: state.data.owner_subject,
        expected_run_revision: state.data.run_revision,
        reason: reason.trim(),
      });
      setTransferOpen(false);
      setSubject('');
      setReason('');
      await Promise.all([state.refetch(), refreshAfterHandover(cache)]);
      notify('Responsibility transferred. Entry approval history and review dates are preserved.');
    } catch (caught) {
      setError(message(caught));
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="page-content stack">
      <div className="page-heading">
        <div>
          <p className="eyebrow">Keep knowledge current</p>
          <h1>Maintenance</h1>
          <p>
            Expert: {run.expert_name}. Current owner: {state.data?.owner_name ?? 'Unassigned'}.
          </p>
        </div>
        {identity.admin && (
          <Button onClick={() => setTransferOpen(true)}>Transfer responsibility</Button>
        )}
      </div>
      {state.data?.owner_unavailable && (
        <Notice error>
          The owner account is disabled. An administrator can transfer responsibility.
        </Notice>
      )}
      {state.data?.maintenance_started_at && (
        <Notice>
          Capture is closed. The current owner reviews and maintains the entries under their own
          identity.
        </Notice>
      )}
      {(error || state.error || queue.error) && (
        <Notice error>
          {error || message(state.error ?? queue.error)}{' '}
          <Button
            onClick={() => {
              void state.refetch();
              void queue.refetch();
            }}
          >
            Refresh
          </Button>
        </Notice>
      )}
      <div className="form-field">
        <label htmlFor="maintenance-filter">Show entries</label>
        <select
          id="maintenance-filter"
          value={filter}
          onChange={event => {
            setFilter(event.target.value);
            setOffset(0);
          }}
        >
          <option value="all">All entries</option>
          <option value="overdue">Review overdue</option>
          <option value="due_soon">Review due soon</option>
          <option value="flagged">Reader feedback pending</option>
        </select>
      </div>
      {queue.isPending ? (
        <Loading />
      ) : queue.data?.entries.length ? (
        <div className="entry-list">
          {queue.data.entries.map(entry => (
            <article className="list-row entry-row" key={entry.id}>
              <div className="grow">
                <h2 className="card-title">
                  <Link to={`/workspace/${run.id}/entries/${entry.id}`}>{entry.title}</Link>
                </h2>
                <p className="small muted">
                  Version {entry.version} · {entry.status}
                </p>
                {entry.status !== 'approved' && Boolean(entry.open_flag_count) && (
                  <p className="small">Reader feedback pending · {entry.open_flag_count}</p>
                )}
                <Freshness value={entry} />
              </div>
              <Link className="btn" to={`/workspace/${run.id}/entries/${entry.id}`}>
                Open entry
              </Link>
            </article>
          ))}
        </div>
      ) : (
        !queue.error && (
          <Empty title="No entries in this view">
            <p>Change the filter to see other entries.</p>
          </Empty>
        )
      )}
      <div className="toolbar">
        {offset > 0 && (
          <Button onClick={() => setOffset(Math.max(0, offset - 50))}>Previous page</Button>
        )}
        {queue.data?.next_offset != null && (
          <Button onClick={() => setOffset(queue.data!.next_offset!)}>Next page</Button>
        )}
      </div>
      {state.data?.history.length ? (
        <details>
          <summary>Responsibility history</summary>
          <ol className="decision-list">
            {state.data.history.map((item, index) => (
              <li key={index}>
                <strong>
                  {item.previous_owner ?? 'Unassigned'} → {item.owner_subject}
                </strong>
                <p>{item.reason}</p>
                <span className="small muted">
                  {item.actor_subject} · {new Date(item.at).toLocaleString()}
                </span>
              </li>
            ))}
          </ol>
        </details>
      ) : null}
      <Modal
        title="Transfer responsibility?"
        open={transferOpen}
        onClose={() => {
          if (!busy) setTransferOpen(false);
        }}
      >
        <form onSubmit={event => void transfer(event)} className="stack">
          <p aria-live="polite">
            <strong>
              From {state.data?.owner_name ?? 'Unassigned'} to{' '}
              {subject.trim() || 'a new owner (enter a subject below)'}.
            </strong>
          </p>
          <p>
            This closes capture and gives the new owner responsibility for entry review. Accepted
            interview work must finish first. Entry history, deadlines and unresolved feedback are
            kept.
          </p>
          {state.data?.summary && (
            <p>
              {state.data.summary.drafts} drafts · {state.data.summary.overdue} overdue entries ·{' '}
              {state.data.summary.open_flags} open reader reports
            </p>
          )}
          <div className="form-field">
            <label htmlFor="successor-subject">New owner subject</label>
            <input
              id="successor-subject"
              required
              maxLength={200}
              value={subject}
              onChange={event => setSubject(event.target.value)}
              disabled={busy}
            />
            <p className="field-help">Use an active account or configured bearer identity.</p>
          </div>
          <div className="form-field">
            <label htmlFor="handover-reason">Reason for transfer</label>
            <textarea
              id="handover-reason"
              required
              maxLength={2000}
              value={reason}
              onChange={event => setReason(event.target.value)}
              disabled={busy}
            />
          </div>
          {error && <Notice error>{error}</Notice>}
          <div className="toolbar">
            <Button variant="primary" type="submit" disabled={busy || !state.data}>
              {busy ? 'Transferring…' : 'Transfer and start maintenance'}
            </Button>
            <Button disabled={busy} onClick={() => setTransferOpen(false)}>
              Cancel
            </Button>
          </div>
        </form>
      </Modal>
    </div>
  );
}
