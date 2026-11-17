import { useFollowUpQueue } from './useFollowUpQueue';
import { Link } from 'react-router-dom';
import { Badge, Button, Empty, Loading, Notice } from '../ui';
import { type FollowUpLimits } from '../types';

import { describe, defaults, limitLabels, gapQuestion, FollowUpLimitSummary } from './shared';
import { TriageDialog } from './TriageDialog';
import { ResolveDialog } from './ResolveDialog';
export function FollowUps() {
  const {
    run,
    filter,
    setFilter,
    offset,
    setOffset,
    selected,
    setSelected,
    setParent,
    limits,
    setLimits,
    editing,
    setEditing,
    resolving,
    setResolving,
    busy,
    error,
    setError,
    queue,
    plans,
    parents,
    entries,
    available,
    parentId,
    changed,
    triage,
    prepare,
  } = useFollowUpQueue();
  if (queue.isPending || plans.isPending || (plans.data && parents.isPending)) return <Loading />;
  return (
    <div className="page-content stack follow-up-page">
      <div>
        <p className="eyebrow">Return to the expert</p>
        <h1>Follow-ups</h1>
        <p className="page-intro">
          Review public gaps and saved reader questions. Select only what the original expert should
          answer; applying and approving their knowledge are separate steps.
        </p>
      </div>
      {!available && (
        <Notice>
          {run.maintenance_started_at
            ? 'Capture is closed after handover. You can review captured proposals and resolve reader requests against approved entries.'
            : 'The original expert is unavailable. Preparing or starting another interview is disabled until an operator restores access.'}
        </Notice>
      )}
      {(error || queue.error || plans.error || parents.error || entries.error) && (
        <Notice error>
          {error || describe(queue.error ?? plans.error ?? parents.error ?? entries.error)}{' '}
          <Button
            onClick={() => {
              void queue.refetch();
              void parents.refetch();
              void entries.refetch();
            }}
          >
            Refresh saved state
          </Button>
        </Notice>
      )}
      <div className="toolbar">
        <div className="form-field">
          <label htmlFor="gap-filter">Show gaps</label>
          <select
            id="gap-filter"
            value={filter}
            onChange={event => {
              setFilter(event.target.value);
              setOffset(0);
              setSelected([]);
            }}
          >
            {[
              ['open', 'Open'],
              ['all', 'All'],
              ['requests', 'Reader requests'],
              ['open_sections', 'Open sections'],
              ['deferred', 'Deferred'],
              ['dismissed', 'Dismissed'],
            ].map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </select>
        </div>
        <Button onClick={() => void queue.refetch()}>Refresh gaps</Button>
      </div>
      {queue.data?.gaps.length ? (
        <div className="stack">
          {queue.data.gaps.map(gap => (
            <article className="card stack" key={gap.id}>
              <div className="inline">
                <Badge>{gap.status.replaceAll('_', ' ')}</Badge>
                <span className="small muted">
                  {gap.kind === 'request'
                    ? 'Saved reader question'
                    : `${gap.entry_title} · ${gap.section}`}{' '}
                  · revision {gap.revision}
                </span>
              </div>
              <h2 className="card-title">{gapQuestion(gap)}</h2>
              {gap.seed_question && gap.question && gap.seed_question !== gap.question && (
                <p className="small muted">Original question: {gap.question}</p>
              )}
              <p className="small muted">
                Priority {gap.priority ?? 0}
                {gap.new_topic
                  ? ` · New topic: ${gap.topic_label}`
                  : gap.target_entry_id || gap.entry_id
                    ? ` · Target: ${entries.data?.find(entry => entry.id === (gap.target_entry_id || gap.entry_id))?.title ?? gap.entry_title ?? 'entry'} · ${gap.target_section || gap.section}`
                    : ' · Map to an entry or choose a new topic before selecting'}
                {gap.group_id && ' · Grouped with another gap; select its leading question'}
              </p>
              {gap.reason && <p className="small">{gap.reason}</p>}
              {gap.resolved_entry_id && (
                <Link to={`/workspace/${run.id}/entries/${gap.resolved_entry_id}`}>
                  Resolved by approved entry · version {gap.resolved_version}
                </Link>
              )}
              {gap.status === 'pending' && !gap.group_id && (
                <label className="inline">
                  <input
                    type="checkbox"
                    checked={selected.includes(gap.id)}
                    disabled={
                      !available ||
                      busy ||
                      (gap.kind === 'request' && !gap.target_entry_id && !gap.new_topic)
                    }
                    aria-label={`Select gap: ${gapQuestion(gap)}`}
                    onChange={event =>
                      setSelected(value =>
                        event.target.checked
                          ? [...value, gap.id]
                          : value.filter(id => id !== gap.id),
                      )
                    }
                  />
                  Select for a follow-up
                </label>
              )}
              <div className="toolbar">
                {['pending', 'deferred'].includes(gap.status) && (
                  <Button disabled={busy} onClick={() => setEditing(gap)}>
                    Triage question
                  </Button>
                )}
                {(['deferred', 'dismissed'].includes(gap.status) ||
                  (gap.kind === 'request' && gap.status === 'resolved')) && (
                  <Button disabled={busy} onClick={() => void triage(gap, 'reopen')}>
                    Reopen gap
                  </Button>
                )}
                {gap.kind === 'request' &&
                  !['resolved', 'scheduled', 'dismissed'].includes(gap.status) && (
                    <Button disabled={busy} onClick={() => setResolving(gap)}>
                      Resolve with approved entry
                    </Button>
                  )}
              </div>
              {gap.attempts?.length > 0 && (
                <details>
                  <summary>
                    {gap.attempts.length} previous{' '}
                    {gap.attempts.length === 1 ? 'attempt' : 'attempts'}
                  </summary>
                  <ul>
                    {gap.attempts.map((attempt, index) => (
                      <li key={index}>
                        <Link to={`/workspace/${run.id}/follow-ups/${attempt.session_id}`}>
                          {attempt.outcome.replaceAll('_', ' ')}
                        </Link>
                        <span className="small muted">
                          {' '}
                          · {new Date(attempt.recorded_at).toLocaleString()}
                        </span>
                      </li>
                    ))}
                  </ul>
                </details>
              )}
            </article>
          ))}
        </div>
      ) : (
        <Empty title="No public gaps to follow up">
          <p>
            Saved reader questions and eligible open sections appear here. Confidential answers are
            excluded.
          </p>
        </Empty>
      )}
      <div className="toolbar">
        {offset > 0 && (
          <Button
            onClick={() => {
              setOffset(Math.max(0, offset - 50));
              setSelected([]);
            }}
          >
            Previous gaps
          </Button>
        )}
        {queue.data?.next_offset !== null && queue.data?.next_offset !== undefined && (
          <Button
            onClick={() => {
              setOffset(queue.data!.next_offset!);
              setSelected([]);
            }}
          >
            Next gaps
          </Button>
        )}
      </div>
      <form className="card stack" onSubmit={event => void prepare(event)}>
        <h2>Prepare a focused interview</h2>
        <p>{selected.length} questions selected. Preparing a plan does not call a model.</p>
        <div className="form-field">
          <label htmlFor="follow-up-parent">Finished original interview</label>
          <select
            id="follow-up-parent"
            value={parentId}
            onChange={event => setParent(event.target.value)}
            disabled={busy || !available}
          >
            {!parents.data?.length && <option value="">Finish the original interview first</option>}
            {parents.data?.map((plan, index) => (
              <option value={plan.session_id!} key={plan.id}>
                Original interview {index + 1}
              </option>
            ))}
          </select>
        </div>
        <div className="follow-up-limits">
          {(Object.keys(defaults) as (keyof FollowUpLimits)[]).map(key => (
            <div className="form-field" key={key}>
              <label htmlFor={`limit-${key}`}>{limitLabels[key]}</label>
              <input
                id={`limit-${key}`}
                type="number"
                min={key === 'embedding_calls' ? 0 : 1}
                max={{ topics: 10, questions: 30, model_calls: 60, embedding_calls: 20 }[key]}
                required
                value={limits[key]}
                disabled={busy || !available}
                onChange={event =>
                  setLimits(value => ({ ...value, [key]: Number(event.target.value) }))
                }
              />
            </div>
          ))}
        </div>
        <FollowUpLimitSummary limits={limits} />
        <div>
          <Button
            type="submit"
            variant="primary"
            disabled={
              !available ||
              busy ||
              !parentId ||
              !selected.length ||
              selected.length > limits.questions
            }
          >
            {busy ? 'Preparing…' : 'Prepare follow-up plan'}
          </Button>
        </div>
        {selected.length > limits.questions && (
          <Notice error>Group or remove questions to fit the selected question limit.</Notice>
        )}
      </form>
      {(plans.data ?? []).some(plan => plan.kind === 'follow_up') && (
        <section className="stack">
          <h2>Saved follow-up work</h2>
          {plans.data
            ?.filter(plan => plan.kind === 'follow_up')
            .map(plan => (
              <article className="list-row" key={plan.id}>
                <div className="grow">
                  <h3 className="card-title">
                    Follow-up{' '}
                    {plan.status === 'draft'
                      ? 'plan'
                      : plan.session_status === 'finished'
                        ? 'completed'
                        : 'in progress'}
                  </h3>
                  <p className="small muted">
                    {plan.status === 'draft'
                      ? 'Review the selected questions before starting.'
                      : 'Captured answers stay private until reviewed and approved.'}
                  </p>
                </div>
                <Link
                  className="btn"
                  to={
                    plan.session_id
                      ? `/workspace/${run.id}/${plan.session_status === 'finished' ? 'follow-ups' : 'interview'}/${plan.session_id}`
                      : `/workspace/${run.id}/plans/${plan.id}`
                  }
                >
                  {plan.session_id
                    ? plan.session_status === 'finished'
                      ? 'Review proposals'
                      : 'Continue interview'
                    : 'Review plan'}
                </Link>
              </article>
            ))}
        </section>
      )}
      <TriageDialog
        key={`triage:${editing?.id ?? 'closed'}`}
        gap={editing}
        gaps={queue.data?.gaps ?? []}
        entries={entries.data ?? []}
        busy={busy}
        error={error}
        onClose={() => {
          if (!busy) setEditing(null);
        }}
        submit={triage}
      />
      <ResolveDialog
        key={`resolve:${resolving?.id ?? 'closed'}`}
        gap={resolving}
        entries={entries.data ?? []}
        onClose={() => setResolving(null)}
        onResolved={async () => {
          setResolving(null);
          await changed();
        }}
        onConflict={async message => {
          setResolving(null);
          setError(message);
          await Promise.all([queue.refetch(), entries.refetch()]);
        }}
      />
    </div>
  );
}
