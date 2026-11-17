import { queryKeys } from '../queries';
import { useQuery } from '@tanstack/react-query';
import { useWorkspace } from '../workspace';
import type { Plan, Session, Receipt } from '../types';
export function useInterviewSession(sessionId: string) {
  const { api } = useWorkspace();
  const sessionQuery = useQuery({
    queryKey: queryKeys.session(sessionId),
    queryFn: () => api.request<Session>(`/sessions/${sessionId}`),
    retry: false,
    refetchOnMount: 'always',
  });
  const receiptsQuery = useQuery({
    queryKey: queryKeys.receipts(sessionId),
    queryFn: () => api.request<Receipt[]>(`/sessions/${sessionId}/messages`),
    retry: false,
    refetchOnMount: 'always',
  });
  const session = sessionQuery.data;
  const planQuery = useQuery({
    queryKey: queryKeys.plan(session?.plan_id),
    queryFn: () => api.request<Plan>(`/plans/${session!.plan_id}`),
    enabled: Boolean(session?.plan_id),
    retry: false,
  });
  return { sessionQuery, receiptsQuery, session, planQuery };
}
