import { queryKeys } from '../queries';
import { useRef, useState, type FormEvent } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useWorkspace } from '../workspace';
import { Badge, Button, Modal, Notice } from '../ui';
import { sections, type Entry, type ReaderFlag, type SectionName } from '../types';

import { dayAfter, expected, Freshness } from './reviewMetadata';
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
    queryKey: queryKeys.flags(entry.id, entry.maintenance_revision),
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
