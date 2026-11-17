import { useState, type FormEvent } from 'react';
import { Button, Modal, Notice } from '../ui';
import { sections, type EntrySummary, type FollowUpGap, type SectionName } from '../types';

import { gapQuestion } from './shared';
export function TriageDialog({
  gap,
  gaps,
  entries,
  busy,
  error,
  onClose,
  submit,
}: {
  gap: FollowUpGap | null;
  gaps: FollowUpGap[];
  entries: EntrySummary[];
  busy: boolean;
  error: string;
  onClose: () => void;
  submit: (gap: FollowUpGap, action: string, body: object) => Promise<void>;
}) {
  const [action, setAction] = useState('map');
  const [target, setTarget] = useState(
    (gap?.kind === 'open_section' ? gap.entry_id : gap?.target_entry_id) || entries[0]?.id || '',
  );
  const [section, setSection] = useState<SectionName>(
    (gap?.kind === 'open_section' ? gap.section : gap?.target_section) || 'rollback',
  );
  const [seed, setSeed] = useState(gap ? gapQuestion(gap) : '');
  const [priority, setPriority] = useState(gap?.priority ?? 0);
  const [reason, setReason] = useState('');
  const [topic, setTopic] = useState(gap?.topic_label || '');
  const [group, setGroup] = useState('');
  function send(event: FormEvent) {
    event.preventDefault();
    if (!gap) return;
    const body =
      action === 'map'
        ? { target_entry_id: target, target_section: section, seed_question: seed, priority }
        : action === 'new_topic'
          ? { topic_label: topic, seed_question: seed, priority }
          : action === 'group'
            ? { group_with: group }
            : { reason };
    void submit(gap, action, body);
  }
  return (
    <Modal title="Triage question" open={Boolean(gap)} onClose={onClose}>
      <form className="stack" onSubmit={send}>
        <p>{gap && gapQuestion(gap)}</p>
        <div className="form-field">
          <label htmlFor="triage-action">Action</label>
          <select
            id="triage-action"
            value={action}
            onChange={event => setAction(event.target.value)}
            disabled={busy}
          >
            <option value="map">Map to entry and section</option>
            {gap?.kind === 'request' && <option value="new_topic">Create a new topic</option>}
            <option value="group">Group with another question</option>
            <option value="defer">Defer</option>
            <option value="dismiss">Dismiss</option>
          </select>
        </div>
        {action === 'map' && (
          <>
            <div className="form-field">
              <label htmlFor="triage-target">Target entry</label>
              <select
                id="triage-target"
                value={target}
                onChange={event => setTarget(event.target.value)}
                required
                disabled={busy || gap?.kind === 'open_section'}
              >
                {!entries.length && <option value="">No eligible entries</option>}
                {entries.map(entry => (
                  <option key={entry.id} value={entry.id}>
                    {entry.title} · v{entry.version}
                  </option>
                ))}
              </select>
            </div>
            <div className="form-field">
              <label htmlFor="triage-section">Target section</label>
              <select
                id="triage-section"
                value={section}
                onChange={event => setSection(event.target.value as SectionName)}
                disabled={busy || gap?.kind === 'open_section'}
              >
                {sections.map(name => (
                  <option key={name}>{name}</option>
                ))}
              </select>
            </div>
            {gap?.kind === 'open_section' && (
              <p className="small muted">
                An open-section question retains its original entry and section.
              </p>
            )}
          </>
        )}
        {action === 'new_topic' && (
          <div className="form-field">
            <label htmlFor="triage-topic">New topic name</label>
            <input
              id="triage-topic"
              maxLength={200}
              required
              value={topic}
              onChange={event => setTopic(event.target.value)}
              disabled={busy}
            />
          </div>
        )}
        {['map', 'new_topic'].includes(action) && (
          <>
            <div className="form-field">
              <label htmlFor="triage-seed">Question for the expert</label>
              <textarea
                id="triage-seed"
                required
                maxLength={2000}
                value={seed}
                onChange={event => setSeed(event.target.value)}
                disabled={busy}
              />
            </div>
            <div className="form-field">
              <label htmlFor="triage-priority">Priority (0–10)</label>
              <input
                id="triage-priority"
                type="number"
                min={0}
                max={10}
                value={priority}
                onChange={event => setPriority(Number(event.target.value))}
                disabled={busy}
              />
            </div>
          </>
        )}
        {action === 'group' && (
          <div className="form-field">
            <label htmlFor="triage-group">Leading question</label>
            <select
              id="triage-group"
              value={group}
              required
              onChange={event => setGroup(event.target.value)}
              disabled={busy}
            >
              <option value="">Choose a pending question</option>
              {gaps
                .filter(item => item.id !== gap?.id && item.status === 'pending' && !item.group_id)
                .map(item => (
                  <option key={item.id} value={item.id}>
                    {gapQuestion(item)}
                  </option>
                ))}
            </select>
          </div>
        )}
        {['defer', 'dismiss'].includes(action) && (
          <div className="form-field">
            <label htmlFor="triage-reason">Reason</label>
            <textarea
              id="triage-reason"
              required
              maxLength={2000}
              value={reason}
              onChange={event => setReason(event.target.value)}
              disabled={busy}
            />
          </div>
        )}
        {error && <Notice error>{error}</Notice>}
        <div className="toolbar">
          <Button type="submit" variant="primary" disabled={busy}>
            Save triage
          </Button>
          <Button disabled={busy} onClick={onClose}>
            Cancel
          </Button>
        </div>
      </form>
    </Modal>
  );
}
