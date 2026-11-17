import type { QueryClient, QueryKey } from '@tanstack/react-query';

// Rest scopes preserve the distinction between a root key and a disabled query's undefined ID.
const scoped =
  <const Name extends string>(name: Name) =>
  <const Scope extends readonly unknown[]>(...scope: Scope) =>
    [name, ...scope] as const;
export const queryKeys = {
  runs: scoped('runs'),
  plans: scoped('plans'),
  plan: scoped('plan'),
  generations: scoped('generations'),
  generation: scoped('generation'),
  session: scoped('session'),
  receipts: scoped('receipts'),
  entries: scoped('entries'),
  entry: scoped('entry'),
  approvedEntries: scoped('approved-entries'),
  indexing: scoped('runbook-indexing'),
  maintenance: scoped('maintenance'),
  maintenanceState: scoped('maintenance-state'),
  flags: scoped('flags'),
  followUpDestinations: scoped('follow-up-destinations'),
  followUpGaps: scoped('follow-up-gaps'),
  followUpParents: scoped('follow-up-parents'),
  followUpTargets: scoped('follow-up-targets'),
  followUpProposals: scoped('follow-up-proposals'),
};
const invalidate = (client: QueryClient, keys: QueryKey[]) =>
  Promise.all(keys.map(queryKey => client.invalidateQueries({ queryKey })));
export const refreshAfterInterview = (client: QueryClient, sessionId: string) =>
  invalidate(client, [
    queryKeys.session(sessionId),
    queryKeys.receipts(sessionId),
    queryKeys.entries(),
    queryKeys.entry(),
    queryKeys.approvedEntries(),
    queryKeys.plans(),
  ]);
export const refreshAfterEntryChange = (client: QueryClient) =>
  invalidate(client, [
    queryKeys.entries(),
    queryKeys.approvedEntries(),
    queryKeys.session(),
    queryKeys.followUpTargets(),
    queryKeys.followUpGaps(),
    queryKeys.followUpProposals(),
  ]);
export const refreshAfterProposalChange = (client: QueryClient, entryId: string) =>
  invalidate(client, [
    queryKeys.entries(),
    queryKeys.entry(entryId),
    queryKeys.approvedEntries(),
    queryKeys.followUpTargets(),
  ]);
export const refreshAfterEntryMaintenance = (client: QueryClient) =>
  invalidate(client, [queryKeys.maintenance(), queryKeys.entries(), queryKeys.approvedEntries()]);
export const refreshAfterHandover = (client: QueryClient) =>
  invalidate(client, [queryKeys.runs(), queryKeys.maintenance()]);

export const refreshAfterGeneration = (
  client: QueryClient,
  runId: string,
  generationId: string,
  completed: boolean,
) =>
  invalidate(client, [
    ...(completed ? [queryKeys.plans(runId)] : []),
    queryKeys.generations(runId),
    queryKeys.generation(runId, generationId),
  ]);
export const refreshAfterPlanDiscard = (client: QueryClient, runId: string) =>
  invalidate(client, [queryKeys.plans(runId), queryKeys.followUpGaps(runId)]);
