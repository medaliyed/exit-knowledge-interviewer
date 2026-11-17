import { useState, type FormEvent } from 'react';
import { Link } from 'react-router-dom';
import { ApiError } from '../api';
import { useWorkspace } from '../workspace';
import { Button, Modal, Notice } from '../ui';
import { type EntrySummary, type FollowUpGap } from '../types';

import { describe, gapQuestion } from './shared';
export function ResolveDialog({
  gap,
  entries,
  onClose,
  onResolved,
  onConflict,
}: {
  gap: FollowUpGap | null;
  entries: EntrySummary[];
  onClose: () => void;
  onResolved: () => Promise<void>;
  onConflict: (message: string) => Promise<void>;
}) {
  const { api, run, notify } = useWorkspace();
  const approved = entries.filter(entry => entry.status === 'approved');
  const [target, setTarget] = useState(gap?.target_entry_id ?? '');
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const entry = approved.find(item => item.id === target);
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!gap || !entry || !confirmed) return;
    setBusy(true);
    setError('');
    try {
      await api.request(`/runs/${run.id}/follow-up-gaps/${gap.id}/resolve`, 'POST', {
        expected_revision: gap.revision,
        entry_id: entry.id,
        expected_version: entry.version,
        confirms_answer: true,
      });
      notify('Reader request resolved against the approved entry.');
      await onResolved();
    } catch (caught) {
      if (caught instanceof ApiError && caught.status === 409) await onConflict(describe(caught));
      else setError(describe(caught));
    } finally {
      setBusy(false);
    }
  }
  return (
    <Modal
      title="Resolve reader request"
      open={Boolean(gap)}
      onClose={() => {
        if (!busy) onClose();
      }}
    >
      <form className="stack" onSubmit={event => void submit(event)}>
        <p>{gap && gapQuestion(gap)}</p>
        <div className="form-field">
          <label htmlFor="resolution-entry">Approved entry that answers this question</label>
          <select
            id="resolution-entry"
            required
            value={target}
            onChange={event => {
              setTarget(event.target.value);
              setConfirmed(false);
            }}
            disabled={busy}
          >
            <option value="">Choose an approved entry</option>
            {approved.map(item => (
              <option value={item.id} key={item.id}>
                {item.title} · v{item.version}
              </option>
            ))}
          </select>
        </div>
        {entry && (
          <Link target="_blank" rel="noopener" to={`/workspace/${run.id}/entries/${entry.id}`}>
            Read approved version {entry.version} (opens a new tab)
          </Link>
        )}
        <label className="inline">
          <input
            type="checkbox"
            checked={confirmed}
            onChange={event => setConfirmed(event.target.checked)}
            disabled={busy || !entry}
          />
          I reviewed this approved version and confirm it answers the reader’s question.
        </label>
        {error && <Notice error>{error}</Notice>}
        <div className="toolbar">
          <Button type="submit" variant="primary" disabled={busy || !entry || !confirmed}>
            Confirm request resolved
          </Button>
          <Button disabled={busy} onClick={onClose}>
            Cancel
          </Button>
        </div>
      </form>
    </Modal>
  );
}
