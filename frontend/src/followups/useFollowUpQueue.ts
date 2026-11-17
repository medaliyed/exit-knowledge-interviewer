import { useWorkspace } from '../workspace';
import { queryKeys } from '../queries';
import { useRef, useState, type FormEvent } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useNavigate } from 'react-router-dom';
import { ApiError } from '../api';
import {
  type EntrySummary,
  type FollowUpGap,
  type FollowUpLimits,
  type Plan,
  type PlanSummary,
  type Session,
} from '../types';

import { describe, defaults } from './shared';
export function useFollowUpQueue() {
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
    queryKey: queryKeys.followUpGaps(run.id, filter, offset),
    queryFn: () =>
      api.request<{ gaps: FollowUpGap[]; next_offset: number | null }>(
        `/runs/${run.id}/follow-up-gaps?filter=${filter}&offset=${offset}`,
      ),
  });
  const plans = useQuery({
    queryKey: queryKeys.plans(run.id),
    queryFn: () => api.request<PlanSummary[]>(`/runs/${run.id}/plans`),
  });
  const parents = useQuery({
    queryKey: queryKeys.followUpParents(run.id, plans.data),
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
    queryKey: queryKeys.followUpTargets(run.id, plans.data),
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
    await cache.invalidateQueries({ queryKey: queryKeys.followUpGaps(run.id) });
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
      await Promise.all([
        changed(),
        cache.invalidateQueries({ queryKey: queryKeys.plans(run.id) }),
      ]);
      cache.setQueryData(queryKeys.plan(plan.id), plan);
      navigate(`/workspace/${run.id}/plans/${plan.id}`);
    } catch (caught) {
      setError(describe(caught));
      await queue.refetch();
    } finally {
      setBusy(false);
    }
  }
  return {
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
  };
}
