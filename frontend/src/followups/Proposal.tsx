import { useProposalReview } from './useProposalReview';
import { Link } from 'react-router-dom';
import { Badge, Button, Loading, Modal, Notice, SectionLabel, Source } from '../ui';
import { type Citation, type Claim, type FollowUpProposal } from '../types';

function Claims({
  claims,
  openSource,
}: {
  claims: Claim[];
  openSource: (citation: Citation) => void;
}) {
  return (
    <div className="stack">
      {claims.map((claim, index) => (
        <div key={index}>
          <p className="claim preserve-lines">{claim.text}</p>
          <div className="source-list">
            {claim.citations.map((citation, at) => (
              <Source
                citation={citation}
                key={at}
                onClick={
                  citation.kind === 'interview_turn' ? () => openSource(citation) : undefined
                }
              />
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}

export function Proposal({
  proposal,
  refresh,
}: {
  proposal: FollowUpProposal;
  refresh: () => Promise<unknown>;
}) {
  const {
    run,
    selection,
    setSelection,
    busy,
    error,
    replaceOpen,
    setReplaceOpen,
    source,
    setSource,
    sourceRequest,
    openSource,
    apply,
    rebase,
  } = useProposalReview(proposal, refresh);
  return (
    <article className="card stack proposal">
      <div>
        <div className="inline">
          <Badge status={proposal.status === 'applied' ? 'approved' : 'draft'}>
            {proposal.status === 'applied' ? 'Applied to draft' : 'Unpublished proposal'}
          </Badge>
          <span className="small muted">
            Proposal revision {proposal.revision} · base v{proposal.base_version} · current entry v
            {proposal.entry_version}
          </span>
        </div>
        <h2>{proposal.title}</h2>
      </div>
      {error && <Notice error>{error}</Notice>}
      {proposal.status === 'applied' ? (
        <>
          <p>
            The selected changes were applied as entry version {proposal.applied_version}. Approval
            is still a separate decision.
          </p>
          <Link
            className="btn btn-primary"
            to={`/workspace/${run.id}/entries/${proposal.entry_id}`}
          >
            Review updated entry
          </Link>
        </>
      ) : (
        <>
          {proposal.stale && (
            <Notice error>
              The entry changed after this proposal was prepared. Compare its current sections, then
              explicitly refresh the proposal before applying.
              <div>
                <Button
                  disabled={busy || !proposal.can_apply || proposal.entry_status === 'rejected'}
                  onClick={() => void rebase()}
                >
                  Refresh proposal to current version
                </Button>
              </div>
              {proposal.entry_status === 'rejected' && (
                <Link to={`/workspace/${run.id}/entries/${proposal.entry_id}`}>
                  Reopen the rejected entry through normal review first
                </Link>
              )}
            </Notice>
          )}
          {!proposal.can_apply && (
            <Notice>
              Your account can inspect this proposal. The current entry reviewer must apply it.
            </Notice>
          )}
          {proposal.sections.map(patch => (
            <section className="stack proposal-section" key={patch.section}>
              <div className="section-heading">
                <h3>
                  <SectionLabel name={patch.section} />
                </h3>
                <label className="inline">
                  <input
                    type="checkbox"
                    checked={Boolean(selection[patch.section])}
                    aria-label={`Apply ${patch.section}`}
                    disabled={busy || proposal.stale || !proposal.can_apply}
                    onChange={event =>
                      setSelection(value => {
                        const next = { ...value };
                        if (event.target.checked) next[patch.section] = 'merge';
                        else delete next[patch.section];
                        return next;
                      })
                    }
                  />
                  Apply section
                </label>
              </div>
              <div className="proposal-comparison">
                <div className="stack">
                  <h4>Current section</h4>
                  <Claims
                    claims={patch.base.claims}
                    openSource={citation => void openSource(citation)}
                  />
                  {patch.base.open_question && (
                    <p className="gap-state">Open: {patch.base.open_question}</p>
                  )}
                  {!patch.base.claims.length && !patch.base.open_question && (
                    <p className="muted">No public statements</p>
                  )}
                </div>
                <div className="stack">
                  <h4>Proposed additions</h4>
                  <Claims claims={patch.added} openSource={citation => void openSource(citation)} />
                  <p className="small muted">
                    {patch.clears_open
                      ? 'Fills the existing open question on approval.'
                      : 'The existing open question is kept.'}
                  </p>
                </div>
              </div>
              {selection[patch.section] && (
                <>
                  <div className="form-field">
                    <label htmlFor={`${proposal.id}-${patch.section}-mode`}>
                      How to apply {patch.section}
                    </label>
                    <select
                      id={`${proposal.id}-${patch.section}-mode`}
                      value={selection[patch.section]}
                      disabled={busy}
                      onChange={event =>
                        setSelection(value => ({
                          ...value,
                          [patch.section]: event.target.value as 'merge' | 'replace',
                        }))
                      }
                    >
                      <option value="merge">Keep current statements and add new statements</option>
                      <option value="replace">
                        Replace current statements with proposed statements
                      </option>
                    </select>
                  </div>
                  <details>
                    <summary>Preview resulting section</summary>
                    <Claims
                      claims={
                        (selection[patch.section] === 'replace' ? patch.replaced : patch.merged)
                          .claims
                      }
                      openSource={citation => void openSource(citation)}
                    />
                  </details>
                </>
              )}
            </section>
          ))}
          {proposal.unsupported.length > 0 && (
            <Notice>
              Check the proposed wording against its citations:{' '}
              {proposal.unsupported.map((flag, index) => (
                <p className="small" key={index}>
                  {flag.section}, statement {flag.claim_index + 1}: {flag.terms.join(', ')}
                </p>
              ))}
            </Notice>
          )}
          <p className="small muted">
            Unchanged sections: {proposal.unchanged_sections.join(', ')}.
          </p>
          <p className="muted">
            Applying removes the entry from reader access until its revised draft is approved.
          </p>
          <div className="toolbar">
            <Button
              variant="primary"
              disabled={
                busy || proposal.stale || !proposal.can_apply || !Object.keys(selection).length
              }
              onClick={() =>
                Object.values(selection).includes('replace') ? setReplaceOpen(true) : void apply()
              }
            >
              {busy ? 'Saving…' : 'Apply selected sections'}
            </Button>
            <Link to={`/workspace/${run.id}/entries/${proposal.entry_id}`}>
              Inspect current entry
            </Link>
          </div>
        </>
      )}
      <Modal
        title="Replace existing statements?"
        open={replaceOpen}
        onClose={() => {
          if (!busy) setReplaceOpen(false);
        }}
      >
        <p>
          Your selected replacement sections remove their current statements. Unselected sections
          are preserved. The result is saved as a draft for separate approval.
        </p>
        <div className="toolbar">
          <Button variant="danger" disabled={busy} onClick={() => void apply()}>
            Replace and apply to draft
          </Button>
          <Button disabled={busy} onClick={() => setReplaceOpen(false)}>
            Keep comparing
          </Button>
        </div>
      </Modal>
      <Modal
        title={source ? `Source answer · turn ${source.turn}` : 'Source answer'}
        open={Boolean(source)}
        onClose={() => {
          sourceRequest.current++;
          setSource(null);
        }}
      >
        {source && (
          <>
            <p className="small muted">Interview {source.session}</p>
            {source.loading ? (
              <Loading />
            ) : source.error ? (
              <Notice error>{source.error}</Notice>
            ) : (
              <p className="preserve-lines">{source.text}</p>
            )}
          </>
        )}
      </Modal>
    </article>
  );
}
