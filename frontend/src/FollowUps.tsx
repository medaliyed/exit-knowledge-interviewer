import { useRef, useState, type FormEvent } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Link, useNavigate, useParams } from 'react-router-dom';
import { ApiError } from './api';
import { useWorkspace } from './workspace';
import { Badge, Button, Empty, Loading, Modal, Notice, SectionLabel, Source } from './ui';
import {
  sections,
  type Citation,
  type Claim,
  type EntrySummary,
  type FollowUpGap,
  type FollowUpLimits,
  type FollowUpProposal,
  type FollowUpReceipt,
  type Plan,
  type PlanSummary,
  type Run,
  type SectionName,
  type Session,
} from './types';

const describe = (error: unknown) =>
  error instanceof ApiError && error.status === 409
    ? 'The saved version changed. Refresh, compare the changes, and try again.'
    : error instanceof Error
      ? error.message
      : 'The change could not be saved. Try again.';
export const gapQuestion = (gap: FollowUpGap) =>
  gap.seed_question ||
  gap.question ||
  gap.open_question ||
  `${gap.section} for ${gap.entry_title ?? 'this entry'}`;
const defaults: FollowUpLimits = { topics: 3, questions: 6, model_calls: 12, embedding_calls: 6 };
const limitLabels = {
  topics: 'Topics',
  questions: 'Questions',
  model_calls: 'Model attempts',
  embedding_calls: 'Embedding attempts',
};

export function FollowUpLimitSummary({ limits }: { limits: FollowUpLimits }) {
  return (
    <p className="small muted">
      Up to {limits.topics} topics · {limits.questions} questions · {limits.model_calls} model
      attempts · {limits.embedding_calls} embedding attempts. These limits are frozen when the
      expert approves.
    </p>
  );
}

export function SaveFollowUp({ question }: { question: string }) {
  const { api, run } = useWorkspace();
  const runs = useQuery({
    queryKey: ['follow-up-destinations'],
    queryFn: () => api.request<Run[]>('/runs'),
  });
  // A global Ask never chooses its recipient implicitly, even when a handover is open.
  const [destination, setDestination] = useState('');
  const [receipt, setReceipt] = useState<FollowUpReceipt | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const ids = useRef(new Map<string, string>());
  const recipient = runs.data?.find(item => item.id === destination);
  async function save() {
    if (!destination || busy) return;
    setBusy(true);
    setError('');
    const key = `${destination}:${question}`;
    const requestId = ids.current.get(key) ?? crypto.randomUUID();
    ids.current.set(key, requestId);
    try {
      setReceipt(
        await api.request<FollowUpReceipt>(`/runs/${destination}/follow-up-requests`, 'POST', {
          request_id: requestId,
          question,
        }),
      );
    } catch (caught) {
      setError(describe(caught));
    } finally {
      setBusy(false);
    }
  }
  async function refresh() {
    if (!receipt) return;
    setBusy(true);
    setError('');
    try {
      setReceipt(await api.request<FollowUpReceipt>(`/follow-up-requests/${receipt.id}`));
    } catch (caught) {
      setError(describe(caught));
    } finally {
      setBusy(false);
    }
  }
  return (
    <section className="stack follow-up-save" aria-label="Save an unanswered question">
      {receipt ? (
        <>
          <p role="status">
            <strong>
              {receipt.status === 'pending'
                ? 'Request saved. It is awaiting owner triage.'
                : receipt.status === 'resolved'
                  ? 'Request resolved.'
                  : 'Request saved.'}
            </strong>
          </p>
          <p className="small">
            Status: {receipt.status.replaceAll('_', ' ')}. This request does not schedule an
            interview or promise an answer.
          </p>
          <Button disabled={busy} onClick={() => void refresh()}>
            Refresh request status
          </Button>
        </>
      ) : (
        <>
          <div className="form-field">
            <label htmlFor="follow-up-destination">Send follow-up request to</label>
            <select
              id="follow-up-destination"
              value={destination}
              disabled={busy || runs.isPending}
              onChange={event => setDestination(event.target.value)}
            >
              <option value="">Choose a handover</option>
              {runs.data?.map(item => (
                <option key={item.id} value={item.id}>
                  {item.expert_name}
                  {item.id === run.id ? ' · currently open' : ''}
                </option>
              ))}
            </select>
          </div>
          {recipient && (
            <p className="small">
              Owner: {recipient.owner_name ?? recipient.expert_name}. They receive your question for
              triage.
            </p>
          )}
          <p className="small muted">
            Save this question only if you want the selected handover’s owner to review it. Your
            question is redacted before storage.
          </p>
          <div>
            <Button disabled={!destination || busy} onClick={() => void save()}>
              {busy ? 'Saving…' : 'Save for a follow-up'}
            </Button>
          </div>
        </>
      )}
      {(error || runs.error) && (
        <Notice error>
          {error || describe(runs.error)}
          {runs.error && <Button onClick={() => void runs.refetch()}>Reload handovers</Button>}
        </Notice>
      )}
    </section>
  );
}

export function FollowUps() {
  const { api, run, notify } = useWorkspace();
  const cache = useQueryClient();
  const navigate = useNavigate();
  const [filter, setFilter] = useState('open');
  const [offset, setOffset] = useState(0);
  const [selected, setSelected] = useState<string[]>([]);
  const [parent, setParent] = useState('');
  const [limits, setLimits] = useState<FollowUpLimits>(defaults);
  const [editing, setEditing] = useState<FollowUpGap | null>(null);
  const [resolving, setResolving] = useState<FollowUpGap | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const prepared = useRef<{ fingerprint: string; id: string } | null>(null);
  const queue = useQuery({
    queryKey: ['follow-up-gaps', run.id, filter, offset],
    queryFn: () =>
      api.request<{ gaps: FollowUpGap[]; next_offset: number | null }>(
        `/runs/${run.id}/follow-up-gaps?filter=${filter}&offset=${offset}`,
      ),
  });
  const plans = useQuery({
    queryKey: ['plans', run.id],
    queryFn: () => api.request<PlanSummary[]>(`/runs/${run.id}/plans`),
  });
  const parents = useQuery({
    queryKey: ['follow-up-parents', run.id, plans.data],
    enabled: Boolean(plans.data),
    queryFn: async () => {
      const captures = (plans.data ?? []).filter(
        plan => plan.session_id && plan.kind !== 'follow_up',
      );
      return (
        await Promise.all(
          captures.map(async plan => ({
            ...plan,
            session_status:
              plan.session_status ??
              (await api.request<Session>(`/sessions/${plan.session_id}`)).status,
          })),
        )
      ).filter(plan => plan.session_status === 'finished');
    },
  });
  const entries = useQuery({
    queryKey: ['follow-up-targets', run.id, plans.data],
    enabled: Boolean(plans.data),
    queryFn: async () => {
      const lists = await Promise.all(
        (plans.data ?? [])
          .filter(plan => plan.session_id)
          .map(plan => api.request<EntrySummary[]>(`/sessions/${plan.session_id}/entries`)),
      );
      return [
        ...new Map(
          lists
            .flat()
            .filter(entry => entry.status !== 'rejected')
            .map(entry => [entry.id, entry]),
        ).values(),
      ];
    },
  });
  const available = !run.maintenance_started_at && !run.owner_unavailable;
  const parentId = parent || parents.data?.[0]?.session_id || '';
  async function changed() {
    setSelected([]);
    await cache.invalidateQueries({ queryKey: ['follow-up-gaps', run.id] });
  }
  async function triage(gap: FollowUpGap, action: string, body: object = {}) {
    setBusy(true);
    setError('');
    try {
      await api.request(`/runs/${run.id}/follow-up-gaps/${gap.id}`, 'PATCH', {
        action,
        expected_revision: gap.revision,
        ...body,
      });
      await changed();
      setEditing(null);
      notify('Gap updated.');
    } catch (caught) {
      setError(describe(caught));
      if (caught instanceof ApiError && caught.status === 409) setEditing(null);
      await queue.refetch();
    } finally {
      setBusy(false);
    }
  }
  async function prepare(event: FormEvent) {
    event.preventDefault();
    if (!selected.length || !parentId || busy) return;
    const body = { parent_session_id: parentId, gap_ids: selected, limits };
    const fingerprint = JSON.stringify(body);
    if (prepared.current?.fingerprint !== fingerprint)
      prepared.current = { fingerprint, id: crypto.randomUUID() };
    setBusy(true);
    setError('');
    try {
      const plan = await api.request<Plan>(`/runs/${run.id}/follow-ups`, 'POST', {
        request_id: prepared.current.id,
        ...body,
      });
      await Promise.all([changed(), cache.invalidateQueries({ queryKey: ['plans', run.id] })]);
      cache.setQueryData(['plan', plan.id], plan);
      navigate(`/workspace/${run.id}/plans/${plan.id}`);
    } catch (caught) {
      setError(describe(caught));
      await queue.refetch();
    } finally {
      setBusy(false);
    }
  }
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

function TriageDialog({
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

function ResolveDialog({
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

export function FollowUpProposals() {
  const { sessionId = '' } = useParams();
  const { api, run } = useWorkspace();
  const proposals = useQuery({
    queryKey: ['follow-up-proposals', sessionId],
    queryFn: () =>
      api.request<{ session_id: string; proposals: FollowUpProposal[] }>(
        `/sessions/${sessionId}/follow-up-proposals`,
      ),
  });
  const session = useQuery({
    queryKey: ['session', sessionId],
    queryFn: () => api.request<Session>(`/sessions/${sessionId}`),
  });
  const entries = useQuery({
    queryKey: ['entries', sessionId],
    queryFn: () => api.request<EntrySummary[]>(`/sessions/${sessionId}/entries`),
  });
  if (proposals.isPending || session.isPending || entries.isPending) return <Loading />;
  if (session.data && session.data.run_id !== run.id)
    return <Empty title="This follow-up belongs to another handover" />;
  return (
    <div className="page-content stack">
      <Link className="back-link" to={`/workspace/${run.id}/follow-ups`}>
        ← Follow-up gaps
      </Link>
      <div>
        <p className="eyebrow">Review captured knowledge</p>
        <h1>Follow-up proposals</h1>
        <p className="page-intro">
          Compare each selected section with its current entry. Applying creates a draft; only
          separate entry approval publishes it.
        </p>
      </div>
      {(proposals.error || session.error || entries.error) && (
        <Notice error>
          {describe(proposals.error ?? session.error ?? entries.error)}{' '}
          <Button
            onClick={() => {
              void proposals.refetch();
              void session.refetch();
              void entries.refetch();
            }}
          >
            Try again
          </Button>
        </Notice>
      )}
      {session.data?.usage?.partial && (
        <Notice>
          Some selected questions remain unresolved. Unknown, confidential, skipped, stopped and
          unanswered questions stay in the gap queue.
        </Notice>
      )}
      {session.data?.status === 'active' && (
        <Notice>
          This follow-up is still in progress.{' '}
          <Link to={`/workspace/${run.id}/interview/${sessionId}`}>Continue interview</Link>
        </Notice>
      )}
      {session.data?.status === 'finished' && !run.maintenance_started_at && (
        <Link to={`/workspace/${run.id}/interview/${sessionId}`}>Read the follow-up interview</Link>
      )}
      {proposals.data?.proposals.map(proposal => (
        <Proposal
          key={`${proposal.id}:${proposal.revision}:${proposal.status}`}
          proposal={proposal}
          refresh={() => proposals.refetch()}
        />
      ))}
      {!proposals.error && proposals.data?.proposals.length === 0 && (
        <Empty title="No public section proposal">
          <p>
            This follow-up may have captured only confidential, unknown or skipped answers, or
            created a deliberately new topic below.
          </p>
        </Empty>
      )}
      {Boolean(entries.data?.length) && (
        <section className="stack">
          <h2>New topic entries</h2>
          {entries.data?.map(entry => (
            <article className="list-row" key={entry.id}>
              <div className="grow">
                <h3>{entry.title}</h3>
                <Badge status={entry.status}>{entry.status}</Badge>
              </div>
              <Link className="btn" to={`/workspace/${run.id}/entries/${entry.id}`}>
                Review new entry
              </Link>
            </article>
          ))}
        </section>
      )}
      <Link to={`/workspace/${run.id}/follow-ups`}>
        Return to gaps and resolve answered reader requests
      </Link>
    </div>
  );
}

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

function Proposal({
  proposal,
  refresh,
}: {
  proposal: FollowUpProposal;
  refresh: () => Promise<unknown>;
}) {
  const { api, identity, run, notify } = useWorkspace();
  const cache = useQueryClient();
  const [selection, setSelection] = useState<Partial<Record<SectionName, 'merge' | 'replace'>>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [replaceOpen, setReplaceOpen] = useState(false);
  const [source, setSource] = useState<{
    session: string;
    turn: number;
    text: string;
    error: string;
    loading: boolean;
  } | null>(null);
  const sourceRequest = useRef(0);
  async function openSource(citation: Citation) {
    if (citation.kind !== 'interview_turn') return;
    const request = ++sourceRequest.current;
    const initial = {
      session: citation.session_id,
      turn: citation.turn_index,
      text: '',
      error: '',
      loading: true,
    };
    setSource(initial);
    try {
      const transcript = await api.request<Session>(`/sessions/${citation.session_id}`);
      const turn = transcript.turns.find(item => item.ordinal === citation.turn_index);
      if (sourceRequest.current === request)
        setSource({
          ...initial,
          loading: false,
          text: turn
            ? turn.confidential
              ? '[confidential]'
              : turn.text
            : 'This source turn is unavailable.',
        });
    } catch (caught) {
      if (sourceRequest.current === request)
        setSource({ ...initial, loading: false, error: describe(caught) });
    }
  }
  async function apply() {
    if (!Object.keys(selection).length) return;
    setBusy(true);
    setError('');
    try {
      await api.request(`/follow-up-proposals/${proposal.id}/apply`, 'POST', {
        applied_by: identity.name,
        sections: selection,
        expected_revision: proposal.revision,
        expected_version: proposal.base_version,
        ...(proposal.maintenance_revision == null
          ? {}
          : { expected_maintenance_revision: proposal.maintenance_revision }),
      });
      await refresh();
      await Promise.all([
        cache.invalidateQueries({ queryKey: ['entries'] }),
        cache.invalidateQueries({ queryKey: ['entry', proposal.entry_id] }),
        cache.invalidateQueries({ queryKey: ['approved-entries'] }),
        cache.invalidateQueries({ queryKey: ['follow-up-targets'] }),
      ]);
      notify('Selected sections applied. Review and approve the updated draft.');
    } catch (caught) {
      setError(describe(caught));
      await refresh();
    } finally {
      setBusy(false);
      setReplaceOpen(false);
    }
  }
  async function rebase() {
    setBusy(true);
    setError('');
    setSelection({});
    try {
      await api.request(`/follow-up-proposals/${proposal.id}/refresh`, 'POST', {
        refreshed_by: identity.name,
        expected_revision: proposal.revision,
        expected_version: proposal.entry_version,
      });
      await refresh();
      notify('Proposal refreshed. Review the comparison again before selecting sections.');
    } catch (caught) {
      setError(describe(caught));
      await refresh();
    } finally {
      setBusy(false);
    }
  }
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
