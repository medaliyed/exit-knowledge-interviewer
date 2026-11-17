import { Modal } from '../ui';
import type { Entry } from '../types';
import { date } from './messages';
export function EntryHistory({
  entry,
  open,
  onClose,
}: {
  entry: Entry;
  open: boolean;
  onClose: () => void;
}) {
  return (
    <Modal title="History" open={open} onClose={onClose}>
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
  );
}
