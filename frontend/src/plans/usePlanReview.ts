import { refreshAfterPlanDiscard, queryKeys } from '../queries';
import { ApiError } from '../api';
import { useEffect, useRef, useState, type FormEvent } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useNavigate, useParams } from 'react-router-dom';
import { useWorkspace } from '../workspace';
import type { Generation, Plan, PlanSummary, Topic } from '../types';
import { message, reviewMessage } from './messages';
export function usePlanReview() {
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
    queryKey: queryKeys.plan(planId),
    queryFn: () => api.request<Plan>(`/plans/${planId}`),
  });
  const generations = useQuery({
    queryKey: queryKeys.generations(run.id),
    queryFn: () => api.request<Generation[]>(`/runs/${run.id}/generations`),
  });
  const summaries = useQuery({
    queryKey: queryKeys.plans(run.id),
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
      cache.setQueryData(queryKeys.plan(planId), updated);
      void cache.invalidateQueries({ queryKey: queryKeys.plans(run.id) });
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
      await cache.invalidateQueries({ queryKey: queryKeys.plans(run.id) });
      void cache.invalidateQueries({ queryKey: queryKeys.plan(planId) });
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
          void cache.invalidateQueries({ queryKey: queryKeys.plans(run.id) });
        }
      }
    } finally {
      if (alive.current && currentPlan.current === planId) setBusy(false);
    }
  }

  async function discard() {
    if (!plan) return;
    setBusy(true);
    try {
      await api.request(`/plans/${plan.id}/discard`, 'POST', {
        expected_revision: plan.revision,
      });
      await refreshAfterPlanDiscard(cache, run.id);
      navigate(`/workspace/${run.id}/follow-ups`);
    } catch (caught) {
      setError(message(caught));
    } finally {
      setBusy(false);
    }
  }
  return {
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
  };
}
