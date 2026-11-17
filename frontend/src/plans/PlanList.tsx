import { refreshAfterGeneration, queryKeys } from '../queries';
import { Questions } from './PlanQuestions';
import { useEffect, useRef, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Link, useNavigate } from 'react-router-dom';
import { useWorkspace } from '../workspace';
import { Badge, Button, Empty, Loading, Notice } from '../ui';
import type { Generation, Plan, PlanSummary, Session as SessionSummary } from '../types';
import { message } from './messages';
function PlanRow({ summary, number }: { summary: PlanSummary; number: number }) {
  const { api, run } = useWorkspace();
  const detail = useQuery({
    queryKey: queryKeys.plan(summary.id),
    queryFn: () => api.request<Plan>(`/plans/${summary.id}`),
  });
  const session = useQuery({
    queryKey: queryKeys.session(summary.session_id),
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
    queryKey: queryKeys.plans(run.id),
    queryFn: () => api.request<PlanSummary[]>(`/runs/${run.id}/plans`),
  });
  const generations = useQuery({
    queryKey: queryKeys.generations(run.id),
    queryFn: () => api.request<Generation[]>(`/runs/${run.id}/generations`),
    refetchInterval: working ? 1500 : false,
  });
  const progress = useQuery({
    queryKey: queryKeys.generation(run.id, activeId),
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
      cache.setQueryData(queryKeys.plan(plan.id), plan);
      await refreshAfterGeneration(cache, run.id, id, true);
      if (alive.current && currentRun.current === run.id) {
        notify('Your plan is ready to review.');
        navigate(`/workspace/${run.id}/plans/${plan.id}`);
      }
    } catch (caught) {
      if (alive.current && currentRun.current === run.id) setError(message(caught));
      await refreshAfterGeneration(cache, run.id, id, false);
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
