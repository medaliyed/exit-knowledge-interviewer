import { useRef, useState, type FormEvent } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { useWorkspace } from './workspace';
import { Badge, Button, Empty, Loading, Modal, Notice } from './ui';
import {
  sections,
  type Entry,
  type EntrySummary,
  type MaintenanceState,
  type ReaderFlag,
  type ReviewMetadata,
  type SectionName,
} from './types';

const message = (error: unknown) =>
  error instanceof Error
    ? error.message
    : 'The maintenance change could not be saved. Refresh and try again.';
const resolutionLabel = (value: ReaderFlag['resolution']) =>
  value === 'confirmed_current'
    ? 'Confirmed current'
    : value === 'corrected'
      ? 'Corrected'
      : value === 'dismissed'
        ? 'Dismissed'
        : 'Awaiting owner review';
export const dayAfter = (days: number) =>
  new Date(Date.now() + days * 86400000).toISOString().slice(0, 10);
export const expected = (entry: EntrySummary) => ({
  expected_version: entry.version,
  expected_maintenance_revision: entry.maintenance_revision,
});

export function Freshness({ value }: { value: ReviewMetadata }) {
  if (!value.review_by || !value.freshness) return null;
  return (
    <div className="review-metadata stack small" aria-label="Review information">
      <div className="inline">
        <Badge status={value.freshness === 'overdue' ? 'rejected' : undefined}>
          {value.freshness === 'overdue'
            ? 'Review overdue'
            : value.freshness === 'due_soon'
              ? 'Review due soon'
              : 'Review current'}
        </Badge>
        <span>Review by {value.review_by} (UTC)</span>
        {Boolean(value.open_flag_count) && (
          <span>Reader feedback pending · {value.open_flag_count}</span>
        )}
      </div>
      {value.last_reviewed_at && (
        <span>
          Last reviewed by {value.last_reviewed_by_name ?? 'Unknown'} ·{' '}
          {value.last_reviewed_at.slice(0, 10)}
        </span>
      )}
      {value.owner_name && <span>Maintained by {value.owner_name}</span>}
    </div>
  );
}

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
    queryKey: ['maintenance-state', run.id],
    queryFn: () => api.request<MaintenanceState>(`/runs/${run.id}/maintenance`),
  });
  const queue = useQuery({
    queryKey: ['maintenance', run.id, filter, offset],
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
      await Promise.all([
        state.refetch(),
        cache.invalidateQueries({ queryKey: ['runs'] }),
        cache.invalidateQueries({ queryKey: ['maintenance'] }),
      ]);
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

export function EntryMaintenance({
  entry,
  manager,
  canReview,
  onChanged,
}: {
  entry: Entry;
  manager: boolean;
  canReview: boolean;
  onChanged: () => Promise<unknown>;
}) {
  const { api, notify } = useWorkspace();
  const [reviewBy, setReviewBy] = useState(dayAfter(90));
  const [reviewOpen, setReviewOpen] = useState(false);
  const [withdrawOpen, setWithdrawOpen] = useState(false);
  const [reason, setReason] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [reportOpen, setReportOpen] = useState(false);
  const [category, setCategory] = useState<ReaderFlag['category']>('outdated');
  const [section, setSection] = useState<SectionName | ''>('');
  const [description, setDescription] = useState('');
  const [saved, setSaved] = useState<ReaderFlag | null>(null);
  const requestId = useRef(crypto.randomUUID());
  const [resolving, setResolving] = useState<ReaderFlag | null>(null);
  const [resolution, setResolution] = useState<NonNullable<ReaderFlag['resolution']>>('dismissed');
  const [note, setNote] = useState('');
  const flags = useQuery({
    queryKey: ['flags', entry.id, entry.maintenance_revision],
    queryFn: () => api.request<{ flags: ReaderFlag[] }>(`/entries/${entry.id}/flags`),
    enabled: manager && entry.maintenance_revision !== undefined,
  });
  async function change(path: string, body: object, done: () => void) {
    setBusy(true);
    setError('');
    try {
      await api.request(path, 'POST', body);
      done();
      await onChanged();
      await flags.refetch();
      notify('Maintenance change saved.');
    } catch (caught) {
      setError(message(caught));
    } finally {
      setBusy(false);
    }
  }
  async function report(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError('');
    try {
      const result = await api.request<ReaderFlag>(`/entries/${entry.id}/flags`, 'POST', {
        request_id: requestId.current,
        expected_version: entry.version,
        category,
        section: section || null,
        description: description.trim(),
      });
      setSaved(result);
      setReportOpen(false);
      setDescription('');
      requestId.current = crypto.randomUUID();
      await onChanged();
    } catch (caught) {
      setError(message(caught));
    } finally {
      setBusy(false);
    }
  }
  const edited = () => {
    requestId.current = crypto.randomUUID();
  };
  return (
    <section className="stack maintenance-controls" aria-label="Entry maintenance">
      <Freshness value={entry} />
      <div className="toolbar">
        {canReview && entry.status === 'approved' && (
          <>
            <Button onClick={() => setReviewOpen(true)}>Reviewed; no changes needed</Button>
            <Button variant="danger" onClick={() => setWithdrawOpen(true)}>
              Withdraw to draft
            </Button>
          </>
        )}
        {entry.status === 'approved' && (
          <Button
            onClick={() => {
              setError('');
              setReportOpen(true);
            }}
          >
            Report a problem
          </Button>
        )}
      </div>
      {error && <Notice error>{error}</Notice>}
      {saved && (
        <div className="card stack" role="status">
          <p>
            Report saved · {resolutionLabel(saved.resolution)}. Receipt: {saved.id}
          </p>
          <Button
            onClick={async () => {
              try {
                setSaved(await api.request<ReaderFlag>(`/flags/${saved.id}`));
              } catch (caught) {
                setError(message(caught));
              }
            }}
          >
            Check report status
          </Button>
        </div>
      )}
      {manager &&
        (flags.error ? (
          <Notice error>
            Reader feedback could not load.{' '}
            <Button onClick={() => void flags.refetch()}>Try again</Button>
          </Notice>
        ) : flags.data?.flags.length ? (
          <section className="stack" aria-label="Reader feedback">
            <h2>Reader feedback</h2>
            {flags.data.flags.map(flag => (
              <article key={flag.id} className="card stack">
                <div className="inline">
                  <Badge>{flag.resolution ? resolutionLabel(flag.resolution) : 'Open'}</Badge>
                  <span>
                    {flag.category} · reported version {flag.observed_version} · current version{' '}
                    {entry.version}
                  </span>
                </div>
                <p className="preserve-lines">{flag.description}</p>
                <p className="small muted">
                  {flag.reporter_subject} · {new Date(flag.created_at).toLocaleString()}
                  {flag.section ? ` · ${flag.section}` : ''}
                </p>
                {flag.resolution_note && <p>Private resolution: {flag.resolution_note}</p>}
                {canReview && !flag.resolution && (
                  <Button
                    onClick={() => {
                      setResolving(flag);
                      setNote('');
                      setError('');
                    }}
                  >
                    Resolve feedback
                  </Button>
                )}
              </article>
            ))}
          </section>
        ) : null)}
      <Modal
        title="Review without content changes?"
        open={reviewOpen}
        onClose={() => {
          if (!busy) setReviewOpen(false);
        }}
      >
        <form
          className="stack"
          onSubmit={event => {
            event.preventDefault();
            void change(
              `/entries/${entry.id}/review`,
              { ...expected(entry), review_by: reviewBy },
              () => setReviewOpen(false),
            );
          }}
        >
          <p>
            Confirm that this version and its sources are still current. The original approval and
            claims are preserved. Reader reports remain open until you resolve them explicitly.
          </p>
          <div className="form-field">
            <label htmlFor="unchanged-review-by">Next review by (UTC)</label>
            <input
              id="unchanged-review-by"
              type="date"
              required
              min={dayAfter(1)}
              max={dayAfter(365)}
              value={reviewBy}
              onChange={event => setReviewBy(event.target.value)}
            />
          </div>
          {error && <Notice error>{error}</Notice>}
          <div className="toolbar">
            <Button type="submit" variant="primary" disabled={busy}>
              Confirm review
            </Button>
            <Button disabled={busy} onClick={() => setReviewOpen(false)}>
              Cancel
            </Button>
          </div>
        </form>
      </Modal>
      <Modal
        title="Withdraw this entry?"
        open={withdrawOpen}
        onClose={() => {
          if (!busy) setWithdrawOpen(false);
        }}
      >
        <form
          className="stack"
          onSubmit={event => {
            event.preventDefault();
            void change(
              `/entries/${entry.id}/withdraw`,
              { ...expected(entry), reason: reason.trim() },
              () => setWithdrawOpen(false),
            );
          }}
        >
          <p>
            This removes the entry from reader access until it is approved again. Its content and
            decision history are preserved.
          </p>
          <div className="form-field">
            <label htmlFor="withdraw-reason">Reason for withdrawal</label>
            <textarea
              id="withdraw-reason"
              required
              maxLength={2000}
              value={reason}
              onChange={event => setReason(event.target.value)}
            />
          </div>
          {error && <Notice error>{error}</Notice>}
          <div className="toolbar">
            <Button type="submit" variant="danger" disabled={busy}>
              Withdraw entry
            </Button>
            <Button disabled={busy} onClick={() => setWithdrawOpen(false)}>
              Cancel
            </Button>
          </div>
        </form>
      </Modal>
      <Modal
        title="Report a problem"
        open={reportOpen}
        onClose={() => {
          if (!busy) setReportOpen(false);
        }}
      >
        <form className="stack" onSubmit={event => void report(event)}>
          <p>
            The maintainer receives your report. It does not change or withdraw this entry. Report
            text is private to you and the owner/admin.
          </p>
          <div className="form-field">
            <label htmlFor="report-category">Problem type</label>
            <select
              id="report-category"
              value={category}
              onChange={event => {
                setCategory(event.target.value as ReaderFlag['category']);
                edited();
              }}
            >
              <option value="outdated">Outdated</option>
              <option value="incorrect">Incorrect</option>
              <option value="unclear">Unclear</option>
            </select>
          </div>
          <div className="form-field">
            <label htmlFor="report-section">Section (optional)</label>
            <select
              id="report-section"
              value={section}
              onChange={event => {
                setSection(event.target.value as SectionName | '');
                edited();
              }}
            >
              <option value="">Whole entry</option>
              {sections.map(name => (
                <option key={name} value={name}>
                  {name}
                </option>
              ))}
            </select>
          </div>
          <div className="form-field">
            <label htmlFor="report-description">Describe the problem</label>
            <textarea
              id="report-description"
              required
              maxLength={2000}
              value={description}
              onChange={event => {
                setDescription(event.target.value);
                edited();
              }}
            />
          </div>
          {error && <Notice error>{error}</Notice>}
          <div className="toolbar">
            <Button type="submit" variant="primary" disabled={busy || !description.trim()}>
              Submit report
            </Button>
            <Button disabled={busy} onClick={() => setReportOpen(false)}>
              Cancel
            </Button>
          </div>
        </form>
      </Modal>
      <Modal
        title="Resolve reader feedback"
        open={resolving !== null}
        onClose={() => {
          if (!busy) setResolving(null);
        }}
      >
        <form
          className="stack"
          onSubmit={event => {
            event.preventDefault();
            if (resolving)
              void change(
                `/entries/${entry.id}/flags/${resolving.id}/resolve`,
                { ...expected(entry), resolution, note: note.trim() },
                () => setResolving(null),
              );
          }}
        >
          <p>
            Corrected requires a content edit after this report, then a later approved version.
            Confirmed current requires an explicit review after this report. Dismissal does not
            renew the review date.
          </p>
          <div className="form-field">
            <label htmlFor="feedback-resolution">Resolution</label>
            <select
              id="feedback-resolution"
              value={resolution}
              onChange={event =>
                setResolution(event.target.value as NonNullable<ReaderFlag['resolution']>)
              }
            >
              <option value="dismissed">Dismissed</option>
              <option value="corrected">Corrected in current approved version</option>
              <option value="confirmed_current">Confirmed current after review</option>
            </select>
          </div>
          <div className="form-field">
            <label htmlFor="feedback-note">Private resolution reason</label>
            <textarea
              id="feedback-note"
              required
              maxLength={2000}
              value={note}
              onChange={event => setNote(event.target.value)}
            />
          </div>
          {error && <Notice error>{error}</Notice>}
          <div className="toolbar">
            <Button type="submit" variant="primary" disabled={busy || !note.trim()}>
              Save resolution
            </Button>
            <Button disabled={busy} onClick={() => setResolving(null)}>
              Cancel
            </Button>
          </div>
        </form>
      </Modal>
    </section>
  );
}
