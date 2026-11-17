import { usePlanReview } from './usePlanReview';
import { FollowUpLimitSummary } from '../followups/shared';
import { Link } from 'react-router-dom';
import { Badge, Button, Empty, Loading, Modal, Notice } from '../ui';
import { message } from './messages';
import { Questions } from './PlanQuestions';
export function PlanReview() {
  const {
    run,
    identity,
    busy,
    error,
    approveOpen,
    setApproveOpen,
    label,
    setLabel,
    note,
    setNote,
    dragging,
    setDragging,
    removing,
    setRemoving,
    planQuery,
    generations,
    summaries,
    plan,
    editable,
    partial,
    ownName,
    followUp,
    respondent,
    edit,
    reorder,
    add,
    remove,
    approve,
    discard,
  } = usePlanReview();
  if (planQuery.isPending) return <Loading />;
  if (!plan)
    return (
      <Notice error>
        {message(planQuery.error)}{' '}
        <Button onClick={() => void planQuery.refetch()}>Try again</Button>
      </Notice>
    );
  if (plan.run_id !== run.id)
    return (
      <Empty title="This plan belongs to another handover">
        <p>Choose a plan from the selected handover.</p>
        <Link className="btn" to={`/workspace/${run.id}/plans`}>
          All plans
        </Link>
      </Empty>
    );
  const summary = summaries.data?.find(item => item.id === plan.id);
  const number = summaries.data
    ? summaries.data.length - summaries.data.findIndex(item => item.id === plan.id)
    : null;
  return (
    <div className="stack page-content">
      <Link className="back-link" to={`/workspace/${run.id}/plans`}>
        ← All plans
      </Link>
      <div className="page-heading">
        <div>
          <div className="inline">
            <h1>
              {followUp
                ? 'Follow-up plan'
                : number && number > 0
                  ? `Plan ${number}`
                  : 'Interview plan'}
            </h1>
            <Badge status={editable ? 'draft' : 'approved'}>
              {editable ? 'Ready for review' : 'Approved'}
            </Badge>
          </div>
          <p className="page-intro">
            {followUp
              ? 'Review these selected questions and sections. The original expert answers them in a focused interview with the limits below.'
              : 'These are the topics the interviewer will ask about, in this order. Move them, remove what does not matter, and add what is missing. Each drafted question shows the words that prompted it.'}
          </p>
          {followUp && plan.limits && <FollowUpLimitSummary limits={plan.limits} />}
        </div>
      </div>
      {error && <Notice error>{error}</Notice>}
      {partial && (
        <Notice error>
          This plan is still being drafted. Finish drafting all topics before approving it.{' '}
          <Link to={`/workspace/${run.id}/plans`}>Return to saved drafting progress</Link>
        </Notice>
      )}
      {!editable && (
        <Notice>
          This plan is frozen. {plan.approved_by && <>Approved by {plan.approved_by}.</>}{' '}
          {summary?.session_id && (
            <Link to={`/workspace/${run.id}/interview/${summary.session_id}`}>
              Open the interview
            </Link>
          )}
        </Notice>
      )}
      {plan.topics.length === 0 && (
        <Empty title="Your plan is empty">
          <p>Add a topic below before approving.</p>
        </Empty>
      )}
      <ol className="topic-list stack">
        {plan.topics.map((topic, index) => (
          <li
            id={`topic-${topic.topic_id}`}
            tabIndex={-1}
            className={`card topic-card ${topic.origin === 'contradiction' ? 'card-warning' : ''}`}
            key={topic.topic_id}
            draggable={editable && !busy}
            onDragStart={event => {
              setDragging(topic.topic_id);
              event.dataTransfer.setData('text/plain', topic.topic_id);
              event.dataTransfer.effectAllowed = 'move';
            }}
            onDragEnd={() => setDragging(null)}
            onDragOver={event => {
              if (editable && dragging) event.preventDefault();
            }}
            onDrop={event => {
              event.preventDefault();
              if (dragging) void reorder(dragging, index);
              setDragging(null);
            }}
          >
            {editable && (
              <div className="reorder-controls" aria-label={`Reorder ${topic.label}`}>
                <span className="drag-handle" aria-hidden="true">
                  ⠿
                </span>
                <Button
                  variant="quiet"
                  disabled={busy || index === 0}
                  aria-label={`Move ${topic.label} up`}
                  onClick={() => void reorder(topic.topic_id, index - 1)}
                >
                  ↑
                </Button>
                <Button
                  variant="quiet"
                  disabled={busy || index === plan.topics.length - 1}
                  aria-label={`Move ${topic.label} down`}
                  onClick={() => void reorder(topic.topic_id, index + 1)}
                >
                  ↓
                </Button>
              </div>
            )}
            <div className="stack grow">
              <div className="inline">
                <span className="eyebrow">Topic {index + 1}</span>
                {topic.origin === 'contradiction' && (
                  <>
                    <Badge status="rejected">Sources contradict each other</Badge>
                    {index === 0 && <Badge>Asked first</Badge>}
                  </>
                )}
                {topic.origin === 'expert' && <Badge>Added by you</Badge>}
              </div>
              <div>
                <h2>{topic.label}</h2>
                <p className="small muted">
                  {topic.kind}
                  {topic.system && ` · ${topic.system}`}
                </p>
              </div>
              {topic.origin === 'expert' ? (
                <blockquote className="quote">
                  <span className="source-label">Your note</span>
                  {topic.reason}
                </blockquote>
              ) : (
                <p className="small">
                  {topic.score !== null && <strong>Risk {topic.score.toFixed(1)} of 100: </strong>}
                  {topic.reason}
                </p>
              )}
              <Questions
                key={`${topic.topic_id}:${plan.revision ?? 1}`}
                topic={topic}
                editable={editable && followUp}
                busy={busy}
                edit={edit}
              />
              {editable && (
                <div>
                  <Button variant="quiet" disabled={busy} onClick={() => setRemoving(topic)}>
                    Remove topic
                  </Button>
                </div>
              )}
            </div>
          </li>
        ))}
      </ol>
      {editable && (
        <>
          {!followUp && (
            <form className="card stack add-topic" onSubmit={event => void add(event)}>
              <h2>Add a topic</h2>
              <div className="form-field">
                <label htmlFor="topic-name">Topic name</label>
                <input
                  id="topic-name"
                  required
                  value={label}
                  disabled={busy}
                  onChange={event => setLabel(event.target.value)}
                />
              </div>
              <div className="form-field">
                <label htmlFor="topic-note">A note in your own words</label>
                <textarea
                  id="topic-note"
                  required
                  value={note}
                  disabled={busy}
                  onChange={event => setNote(event.target.value)}
                  aria-describedby="topic-note-help"
                />
                <p className="field-help" id="topic-note-help">
                  What should the interviewer know before asking? A sentence is enough.
                </p>
              </div>
              <div>
                <Button type="submit" disabled={busy || !label.trim() || !note.trim()}>
                  Add topic
                </Button>
              </div>
            </form>
          )}
          {followUp && !respondent && (
            <Notice error>
              {run.owner_unavailable
                ? 'The original expert is unavailable.'
                : 'Only the original expert signed in as the current owner can start this follow-up.'}{' '}
              Preparing a plan does not change who can answer.
            </Notice>
          )}
          {!ownName && (
            <Notice error>
              You are signed in as {identity.name}. Only the handover expert, {run.expert_name}, can
              approve this plan. You can still review it.
            </Notice>
          )}
          <div className="page-actions">
            <p className="muted">
              Approving freezes the plan and opens the interview. Topics cannot be changed
              afterwards.
            </p>
            <Button
              variant="primary"
              disabled={
                busy ||
                partial ||
                !plan.topics.length ||
                generations.isPending ||
                generations.isError ||
                (followUp && !respondent)
              }
              onClick={() => setApproveOpen(true)}
            >
              Approve plan
            </Button>
          </div>
          {followUp && (
            <Button disabled={busy} onClick={() => void discard()}>
              Discard follow-up plan and release gaps
            </Button>
          )}
          {generations.isError && (
            <Notice error>
              Drafting status could not be checked.{' '}
              <Button variant="quiet" onClick={() => void generations.refetch()}>
                Check again
              </Button>
            </Notice>
          )}
        </>
      )}
      <Modal
        title="Remove this topic?"
        open={removing !== null}
        onClose={() => {
          if (!busy) setRemoving(null);
        }}
      >
        {removing && (
          <>
            <p>
              <strong>{removing.label}</strong> will be taken out of the plan
              {removing.questions.length
                ? `, together with its ${removing.questions.length} drafted ${removing.questions.length === 1 ? 'question' : 'questions'}`
                : ''}
              . The interviewer will not ask about it.
            </p>
            <p className="small muted">
              If you add the topic again later, it starts without drafted questions.
            </p>
            <div className="toolbar">
              <Button variant="primary" disabled={busy} onClick={() => setRemoving(null)}>
                Keep topic
              </Button>
              <Button variant="danger" disabled={busy} onClick={() => void remove()}>
                {busy ? 'Removing…' : 'Remove topic'}
              </Button>
            </div>
          </>
        )}
      </Modal>
      <Modal
        title="Approve this plan?"
        open={approveOpen}
        onClose={() => {
          if (!busy) setApproveOpen(false);
        }}
      >
        <p>Approving starts the interview. The plan cannot be changed afterwards.</p>
        <p className="small muted">
          {plan.topics.length} topics, starting with {plan.topics[0]?.label}.
        </p>
        {!ownName && (
          <Notice error>Your signed-in name must match {run.expert_name} to approve.</Notice>
        )}
        <div className="toolbar">
          <Button disabled={busy} onClick={() => setApproveOpen(false)}>
            Keep editing
          </Button>
          <Button variant="primary" disabled={busy} onClick={() => void approve()}>
            {busy ? 'Opening interview…' : 'Approve and start'}
          </Button>
        </div>
      </Modal>
    </div>
  );
}
