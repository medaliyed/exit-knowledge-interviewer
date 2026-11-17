import { useEntryReview } from './useEntryReview';
import { EntryHistory } from './EntryHistory';
import { EntrySectionEditor } from './EntrySectionEditor';
import { Link } from 'react-router-dom';
import { Badge, Button, Loading, Modal, Notice, SectionLabel } from '../ui';
import { EntryMaintenance } from './EntryMaintenance';
import { dayAfter } from './reviewMetadata';
import { sections } from '../types';

import { errorMessage, date } from './messages';
import { ClaimContent } from './ClaimContent';
export function EntryView() {
  const {
    run,
    identity,
    entryQuery,
    entry,
    manager,
    canReview,
    busy,
    error,
    historyOpen,
    setHistoryOpen,
    reviewBy,
    setReviewBy,
    rejectOpen,
    setRejectOpen,
    reason,
    setReason,
    reasonError,
    setReasonError,
    editing,
    setEditing,
    claims,
    setClaims,
    openQuestion,
    setOpenQuestion,
    editorError,
    sourceOpen,
    setSourceOpen,
    sourceLoading,
    sourceText,
    sourceLabel,
    sourceError,
    editSection,
    saveSection,
    approve,
    reject,
    openSource,
    download,
    maintenanceChanged,
  } = useEntryReview();
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
        onChanged={maintenanceChanged}
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
      <EntryHistory entry={entry} open={historyOpen} onClose={() => setHistoryOpen(false)} />
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
      <EntrySectionEditor
        entryStatus={entry.status}
        editing={editing}
        busy={busy}
        claims={claims}
        setClaims={setClaims}
        openQuestion={openQuestion}
        setOpenQuestion={setOpenQuestion}
        editorError={editorError}
        error={error}
        saveSection={saveSection}
        onClose={() => setEditing(null)}
      />
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
