import { useEffect, useRef, useState, type FormEvent } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Link, useNavigate, useParams } from 'react-router-dom';
import { useWorkspace } from './workspace';
import { ApiError } from './api';
import { Badge, Button, Empty, Loading, Modal, Notice } from './ui';
import { FollowUpLimitSummary } from './FollowUps';
import type { Generation, Plan, PlanSummary, Topic, Session as SessionSummary } from './types';
const message = (error: unknown) =>
  error instanceof Error ? error.message : 'The request could not be completed. Please try again.';
const reviewMessage = (error: unknown) =>
  error instanceof ApiError && error.status === 409
    ? 'This plan changed or is no longer editable. Refresh and review it before trying again.'
    : message(error);

function Questions({
  topic,
  editable = false,
  busy = false,
  edit,
}: {
  topic: Topic;
  editable?: boolean;
  busy?: boolean;
  edit?: (body: object, success: string) => Promise<boolean>;
}) {
  return (
    <div className="stack topic-questions">
      {topic.questions.length === 0 && (
        <p className="muted">
          {topic.origin === 'expert'
            ? 'The interviewer starts from your note; no questions are drafted in advance.'
            : 'No drafted questions passed the source checks. The interviewer will ask about this topic directly.'}
        </p>
      )}
      {topic.questions.map((question, index) => (
        <div key={index}>
          <p className="claim question-text">
            <strong>Q</strong> {question.text}
          </p>
          {question.origin === 'follow_up' && (
            <>
              <p className="small muted">
                Selected section: <span>{question.section ?? 'new topic'}</span>
              </p>
              {editable && edit && question.gap_id && (
                <form
                  className="stack"
                  onSubmit={event => {
                    event.preventDefault();
                    const text = new FormData(event.currentTarget).get('seed') as string;
                    void edit(
                      { seeds: { [question.gap_id!]: text } },
                      'Follow-up question updated.',
                    );
                  }}
                >
                  <label htmlFor={`seed-${question.gap_id}`}>Edit question {index + 1}</label>
                  <textarea
                    id={`seed-${question.gap_id}`}
                    name="seed"
                    defaultValue={question.text}
                    required
                    maxLength={2000}
                    disabled={busy}
                  />
                  <div className="toolbar">
                    <Button type="submit" disabled={busy}>
                      Save question
                    </Button>
                    <Button
                      disabled={busy}
                      onClick={() =>
                        void edit(
                          { remove_gaps: [question.gap_id] },
                          'Question removed and its gap returned to triage.',
                        )
                      }
                    >
                      Remove question
                    </Button>
                  </div>
                </form>
              )}
            </>
          )}
          {question.quote && (
            <blockquote className="quote">
              <span className="source-label">{question.artifact_id}</span>
              <q>{question.quote}</q>
            </blockquote>
          )}
        </div>
      ))}
      {topic.dropped.length > 0 && (
        <details className="dropped-questions">
          <summary>
            {topic.dropped.length} drafted{' '}
            {topic.dropped.length === 1 ? 'question was' : 'questions were'} left out
          </summary>
          <ul>
            {topic.dropped.map((reason, index) => (
              <li className="muted" key={index}>
                {reason}
              </li>
            ))}
          </ul>
        </details>
      )}
    </div>
  );
}

function PlanRow({ summary, number }: { summary: PlanSummary; number: number }) {
  const { api, run } = useWorkspace();
  const detail = useQuery({
    queryKey: ['plan', summary.id],
    queryFn: () => api.request<Plan>(`/plans/${summary.id}`),
  });
  const session = useQuery({
    queryKey: ['session', summary.session_id],
    queryFn: () => api.request<SessionSummary>(`/sessions/${summary.session_id}`),
    enabled: Boolean(summary.session_id),
  });
  const finished = session.data?.status === 'finished' || summary.session_status === 'finished';
  const followUp = detail.data?.kind === 'follow_up' || summary.kind === 'follow_up';
  const label = summary.session_id
    ? finished
      ? 'Interview finished'
      : 'Interview in progress'
    : 'Ready for review';
  const contradictions =
    detail.data?.topics.filter(topic => topic.origin === 'contradiction').length ?? 0;
  return (
    <article className="list-row plan-row">
      <div className="grow">
        <h2 className="card-title">
          {followUp ? 'Follow-up plan' : 'Plan'} {number}
        </h2>
        <p className="small muted">
          {detail.data ? `${detail.data.topics.length} topics` : 'Loading topics…'}
          {contradictions > 0 &&
            ` · ${contradictions} ${contradictions === 1 ? 'contradiction' : 'contradictions'} between sources`}
        </p>
        {session.data && (
          <p className="small muted">
            {finished
              ? `${session.data.notes.filter(note => !note.confidential).length} notes captured · ${session.data.open_items.length} left open`
              : `Topic ${Math.max(1, (detail.data?.topics.findIndex(topic => topic.topic_id === session.data?.current_topic_id) ?? 0) + 1)} of ${detail.data?.topics.length ?? '…'} · accepted answers are saved`}
          </p>
        )}
        {(detail.error || session.error) && (
          <p className="small error-text">
            Some details could not be loaded. Open the plan to try again.
          </p>
        )}
      </div>
      <Badge status={finished ? 'approved' : summary.session_id ? 'progress' : 'draft'}>
        {label}
      </Badge>
      <Link
        className="btn"
        to={
          summary.session_id
            ? finished
              ? followUp
                ? `/workspace/${run.id}/follow-ups/${summary.session_id}`
                : `/workspace/${run.id}/review?session=${summary.session_id}`
              : `/workspace/${run.id}/interview/${summary.session_id}`
            : `/workspace/${run.id}/plans/${summary.id}`
        }
      >
        {summary.session_id
          ? finished
            ? followUp
              ? 'Review proposals'
              : 'Review entries'
            : 'Continue interview'
          : 'Review plan'}{' '}
        <span aria-hidden="true">→</span>
      </Link>
      {summary.session_id && finished && !followUp && (
        <Link className="text-link small" to={`/workspace/${run.id}/follow-ups`}>
          Plan a follow-up
        </Link>
      )}
      {summary.session_id && (
        <Link className="text-link small" to={`/workspace/${run.id}/plans/${summary.id}`}>
          View plan
        </Link>
      )}
    </article>
  );
}

export function Plans() {
  const { api, run, notify } = useWorkspace();
  const cache = useQueryClient();
  const navigate = useNavigate();
  const [activeId, setActiveId] = useState<string | null>(null);
  const [working, setWorking] = useState(false);
  const [error, setError] = useState('');
  const alive = useRef(true);
  const currentRun = useRef(run.id);
  currentRun.current = run.id;
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);
  useEffect(() => {
    setActiveId(null);
    setWorking(false);
    setError('');
  }, [run.id]);
  const plans = useQuery({
    queryKey: ['plans', run.id],
    queryFn: () => api.request<PlanSummary[]>(`/runs/${run.id}/plans`),
  });
  const generations = useQuery({
    queryKey: ['generations', run.id],
    queryFn: () => api.request<Generation[]>(`/runs/${run.id}/generations`),
    refetchInterval: working ? 1500 : false,
  });
  const progress = useQuery({
    queryKey: ['generation', run.id, activeId],
    queryFn: () => api.request<Generation>(`/runs/${run.id}/generations/${activeId}`),
    enabled: Boolean(activeId),
    retry: false,
    refetchInterval: query => (query.state.data?.status === 'completed' ? false : 1500),
  });

  async function draft(id: string, resume: boolean) {
    setActiveId(id);
    setWorking(true);
    setError('');
    try {
      const path = resume
        ? `/runs/${run.id}/generations/${id}/resume`
        : `/runs/${run.id}/plan?generation_id=${id}`;
      const plan = await api.request<Plan>(path, 'POST');
      cache.setQueryData(['plan', plan.id], plan);
      await Promise.all([
        cache.invalidateQueries({ queryKey: ['plans', run.id] }),
        cache.invalidateQueries({ queryKey: ['generations', run.id] }),
        cache.invalidateQueries({ queryKey: ['generation', run.id, id] }),
      ]);
      if (alive.current && currentRun.current === run.id) {
        notify('Your plan is ready to review.');
        navigate(`/workspace/${run.id}/plans/${plan.id}`);
      }
    } catch (caught) {
      if (alive.current && currentRun.current === run.id) setError(message(caught));
      await Promise.all([
        cache.invalidateQueries({ queryKey: ['generation', run.id, id] }),
        cache.invalidateQueries({ queryKey: ['generations', run.id] }),
      ]);
    } finally {
      if (alive.current && currentRun.current === run.id) setWorking(false);
    }
  }

  if (plans.isPending || generations.isPending) return <Loading />;
  return (
    <div className="stack page-content">
      <div className="page-heading">
        <div>
          <p className="eyebrow">Prepare the conversation</p>
          <h1>Interview plans</h1>
          <p className="page-intro">
            A plan is the list of topics the interviewer will ask you about. You review and approve
            it before the conversation starts.
          </p>
        </div>
        <Button
          variant="primary"
          disabled={working || Boolean(run.maintenance_started_at)}
          onClick={() => void draft(crypto.randomUUID(), false)}
        >
          {working ? 'Drafting…' : 'Draft a new plan'}
        </Button>
      </div>
      {(plans.error || generations.error) && (
        <Notice error>
          {message(plans.error ?? generations.error)}{' '}
          <Button
            variant="quiet"
            onClick={() => {
              void plans.refetch();
              void generations.refetch();
            }}
          >
            Try again
          </Button>
        </Notice>
      )}
      {error && (
        <Notice error>
          {error}
          <p className="small">
            Saved topics are kept. If the inputs or configuration changed, draft a new plan.
          </p>
        </Notice>
      )}
      {activeId && (
        <section className="card stack" aria-label="Drafting progress" aria-live="polite">
          <div className="inline">
            <h2>Preparing your conversation</h2>
            <Badge status={progress.data?.status === 'needs_retry' ? 'rejected' : 'progress'}>
              {progress.data?.status === 'needs_retry'
                ? 'Drafting stopped'
                : progress.data?.status === 'completed'
                  ? 'Ready for review'
                  : 'Drafting'}
            </Badge>
          </div>
          {progress.data ? (
            <>
              <p>
                <strong>
                  {progress.data.completed} of {progress.data.total} topics drafted.
                </strong>{' '}
                {progress.data.status === 'needs_retry'
                  ? 'Resume to continue. Completed topics are kept.'
                  : progress.data.status === 'completed'
                    ? 'All topics are saved. Your plan is ready for review.'
                    : 'Completed topics are saved as drafting progresses. You can leave this page and reopen the saved progress.'}
              </p>
              <progress
                aria-label="Topics drafted"
                value={progress.data.completed}
                max={Math.max(progress.data.total, 1)}
              />
              <details>
                <summary>See topics and saved questions</summary>
                <div className="stack">
                  {progress.data.topics.map(topic => (
                    <article className="generation-topic stack" key={topic.topic_id}>
                      <div className="inline">
                        <h3>{topic.label}</h3>
                        <Badge>
                          {topic.generation_state === 'completed' ? 'Saved' : 'Waiting'}
                        </Badge>
                      </div>
                      {topic.generation_state === 'completed' && <Questions topic={topic} />}
                    </article>
                  ))}
                </div>
              </details>
              <div className="toolbar">
                {progress.data.plan_id ? (
                  <Link
                    className="btn btn-primary"
                    to={`/workspace/${run.id}/plans/${progress.data.plan_id}`}
                  >
                    Review completed plan
                  </Link>
                ) : (
                  <Button
                    disabled={working}
                    variant="primary"
                    onClick={() => void draft(activeId, true)}
                  >
                    {working ? 'Drafting…' : 'Resume drafting'}
                  </Button>
                )}
                <Button variant="quiet" onClick={() => void progress.refetch()}>
                  Refresh progress
                </Button>
              </div>
            </>
          ) : (
            <p className="muted">
              {working
                ? 'Starting drafting. This can take a few minutes.'
                : 'Saved progress could not be loaded yet.'}
            </p>
          )}
          {!working && !progress.data && (
            <Button onClick={() => void progress.refetch()}>Check saved progress</Button>
          )}
        </section>
      )}
      {generations.data
        ?.filter(item => item.status !== 'completed' && item.id !== activeId)
        .map(item => (
          <article key={item.id} className="card stack">
            <div className="inline">
              <h2>Saved plan draft</h2>
              <Badge status={item.status === 'needs_retry' ? 'rejected' : 'progress'}>
                {item.status === 'needs_retry' ? 'Drafting stopped' : 'Drafting'}
              </Badge>
            </div>
            <p>
              {item.completed} of {item.total} topics saved.
            </p>
            <div className="toolbar">
              <Button onClick={() => setActiveId(item.id)}>Inspect progress</Button>
              <Button disabled={working} variant="quiet" onClick={() => void draft(item.id, true)}>
                Resume drafting
              </Button>
            </div>
          </article>
        ))}
      {(plans.data?.length ?? 0) > 0 ? (
        <section>
          <h2 className="section-title">Your plans</h2>
          {plans.data?.map((plan, index) => (
            <PlanRow key={plan.id} summary={plan} number={(plans.data?.length ?? 0) - index} />
          ))}
        </section>
      ) : (
        !generations.data?.length &&
        !working && (
          <Empty title="No plan yet">
            <p>Draft a plan from the work already prepared for this handover.</p>
            <p className="small">Source preparation is handled by your operator before drafting.</p>
          </Empty>
        )
      )}
    </div>
  );
}

export function PlanReview() {
  const { planId = '' } = useParams();
  const { api, identity, run, notify } = useWorkspace();
  const cache = useQueryClient();
  const navigate = useNavigate();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [approveOpen, setApproveOpen] = useState(false);
  const [label, setLabel] = useState('');
  const [note, setNote] = useState('');
  const [dragging, setDragging] = useState<string | null>(null);
  const [removing, setRemoving] = useState<Topic | null>(null);
  const alive = useRef(true);
  const currentPlan = useRef(planId);
  currentPlan.current = planId;
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);
  useEffect(() => {
    setBusy(false);
    setError('');
    setApproveOpen(false);
    setLabel('');
    setNote('');
    setDragging(null);
    setRemoving(null);
  }, [planId]);
  const planQuery = useQuery({
    queryKey: ['plan', planId],
    queryFn: () => api.request<Plan>(`/plans/${planId}`),
  });
  const generations = useQuery({
    queryKey: ['generations', run.id],
    queryFn: () => api.request<Generation[]>(`/runs/${run.id}/generations`),
  });
  const summaries = useQuery({
    queryKey: ['plans', run.id],
    queryFn: () => api.request<PlanSummary[]>(`/runs/${run.id}/plans`),
  });
  const plan = planQuery.data;
  const editable = !run.maintenance_started_at && plan?.status === 'draft';
  const partial = generations.data?.some(item => item.id === planId && item.status !== 'completed');
  const ownName = identity.name.trim() === run.expert_name.trim();
  const followUp = plan?.kind === 'follow_up';
  const respondent =
    ownName &&
    !run.owner_unavailable &&
    (run.owner_subject === undefined || run.owner_subject === identity.subject);

  async function edit(body: object, success: string) {
    if (!plan || busy) return false;
    setBusy(true);
    setError('');
    try {
      const updated = await api.request<Plan>(`/plans/${planId}`, 'PATCH', {
        ...body,
        expected_revision: plan.revision,
      });
      cache.setQueryData(['plan', planId], updated);
      void cache.invalidateQueries({ queryKey: ['plans', run.id] });
      notify(success);
      return true;
    } catch (caught) {
      setError(reviewMessage(caught));
      if (caught instanceof ApiError && caught.status === 409) {
        setRemoving(null);
        setDragging(null);
      }
      void planQuery.refetch();
      return false;
    } finally {
      setBusy(false);
    }
  }
  async function reorder(id: string, target: number) {
    if (!plan || busy || !editable) return;
    const order = plan.topics.map(topic => topic.topic_id);
    const from = order.indexOf(id);
    if (from < 0 || target < 0 || target >= order.length || from === target) return;
    order.splice(from, 1);
    order.splice(target, 0, id);
    if (await edit({ order }, `Topic moved to position ${target + 1}.`)) {
      requestAnimationFrame(() =>
        document.getElementById(`topic-${id}`)?.focus({ preventScroll: true }),
      );
    }
  }
  async function add(event: FormEvent) {
    event.preventDefault();
    if (!label.trim() || !note.trim()) return;
    if (
      await edit({ add: [{ label: label.trim(), note: note.trim() }] }, 'Topic added to the plan.')
    ) {
      setLabel('');
      setNote('');
    }
  }
  async function remove() {
    if (!removing || busy) return;
    const topic = removing;
    if (await edit({ remove: [topic.topic_id] }, `“${topic.label}” removed from the plan.`))
      setRemoving(null);
  }
  async function approve() {
    if (!plan || partial || !plan.topics.length || busy) return;
    setBusy(true);
    setError('');
    try {
      const session = await api.request<{ id: string }>(`/plans/${planId}/approve`, 'POST', {
        approved_by: identity.name,
        expected_revision: plan.revision,
      });
      await cache.invalidateQueries({ queryKey: ['plans', run.id] });
      void cache.invalidateQueries({ queryKey: ['plan', planId] });
      if (alive.current && currentPlan.current === planId) {
        setApproveOpen(false);
        notify('Plan approved. Your interview is ready.');
        navigate(`/workspace/${run.id}/interview/${session.id}`);
      }
    } catch (caught) {
      if (alive.current && currentPlan.current === planId) {
        setApproveOpen(false);
        setError(reviewMessage(caught));
        if (caught instanceof ApiError && caught.status === 409) {
          void planQuery.refetch();
          void cache.invalidateQueries({ queryKey: ['plans', run.id] });
        }
      }
    } finally {
      if (alive.current && currentPlan.current === planId) setBusy(false);
    }
  }

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
            <Button
              disabled={busy}
              onClick={async () => {
                setBusy(true);
                try {
                  await api.request(`/plans/${plan.id}/discard`, 'POST', {
                    expected_revision: plan.revision,
                  });
                  await cache.invalidateQueries({ queryKey: ['plans', run.id] });
                  await cache.invalidateQueries({ queryKey: ['follow-up-gaps', run.id] });
                  navigate(`/workspace/${run.id}/follow-ups`);
                } catch (caught) {
                  setError(message(caught));
                } finally {
                  setBusy(false);
                }
              }}
            >
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
