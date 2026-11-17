import type { Dispatch, FormEvent, SetStateAction } from 'react';
import { Modal, Notice, Button } from '../ui';
import type { Entry, SectionName } from '../types';
export function EntrySectionEditor({
  entryStatus,
  editing,
  busy,
  claims,
  setClaims,
  openQuestion,
  setOpenQuestion,
  editorError,
  error,
  saveSection,
  onClose,
}: {
  entryStatus: Entry['status'];
  editing: SectionName | null;
  busy: boolean;
  claims: string[];
  setClaims: Dispatch<SetStateAction<string[]>>;
  openQuestion: string;
  setOpenQuestion: (value: string) => void;
  editorError: string;
  error: string;
  saveSection: (event: FormEvent) => Promise<void>;
  onClose: () => void;
}) {
  return (
    <Modal
      title={`Edit ${editing ?? 'section'}`}
      open={editing !== null}
      onClose={() => {
        if (!busy) onClose();
      }}
    >
      <form className="stack section-editor" onSubmit={event => void saveSection(event)}>
        {entryStatus === 'approved' && (
          <Notice error>
            Saving removes this approved entry from reader access until you approve the updated
            draft.
          </Notice>
        )}
        <p className="small muted">
          Write each statement in your own words. Your edit is saved as a new interview turn, which
          becomes its source.
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
                    onClick={() => setClaims(previous => previous.filter((_, at) => at !== index))}
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
            Keep an honest gap when the answer is unknown. Every section needs at least a statement
            or an open question.
          </p>
        </div>
        {editorError && <Notice error>{editorError}</Notice>}
        {error && <Notice error>{error}</Notice>}
        <div className="toolbar">
          <Button variant="primary" type="submit" disabled={busy}>
            {busy ? 'Saving…' : 'Save section'}
          </Button>
          <Button disabled={busy} onClick={() => onClose()}>
            Cancel
          </Button>
        </div>
      </form>
    </Modal>
  );
}
