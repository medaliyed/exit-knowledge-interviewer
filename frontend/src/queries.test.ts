import { QueryClient } from '@tanstack/react-query';
import { describe, expect, it } from 'vitest';
import { queryKeys, refreshAfterInterview, refreshAfterEntryChange } from './queries';

const client = () => new QueryClient({ defaultOptions: { queries: { gcTime: Infinity } } });
describe('workflow cache refresh rules', () => {
  it('refreshes a delivered session and review data without making another session or identity stale', async () => {
    const cache = client();
    const keys = [
      queryKeys.session('one'),
      queryKeys.session('two'),
      queryKeys.receipts('one'),
      queryKeys.entries('one'),
      queryKeys.entry('entry'),
      queryKeys.approvedEntries('run'),
      queryKeys.runs('owner'),
    ];
    for (const key of keys) cache.setQueryData(key, { saved: true });
    await refreshAfterInterview(cache, 'one');
    expect(cache.getQueryState(queryKeys.session('one'))?.isInvalidated).toBe(true);
    expect(cache.getQueryState(queryKeys.receipts('one'))?.isInvalidated).toBe(true);
    expect(cache.getQueryState(queryKeys.entry('entry'))?.isInvalidated).toBe(true);
    expect(cache.getQueryState(queryKeys.approvedEntries('run'))?.isInvalidated).toBe(true);
    expect(cache.getQueryState(queryKeys.session('two'))?.isInvalidated).toBe(false);
    expect(cache.getQueryState(queryKeys.runs('owner'))?.isInvalidated).toBe(false);
    cache.clear();
  });
  it('makes proposal comparisons and follow-up target choices stale when an entry changes', async () => {
    const cache = client();
    const target = queryKeys.followUpTargets('run', [{ id: 'plan' }]);
    const proposal = queryKeys.followUpProposals('session');
    const gaps = queryKeys.followUpGaps('run', 'open', 0);
    for (const key of [target, proposal, gaps]) cache.setQueryData(key, { version: 1 });
    await refreshAfterEntryChange(cache);
    for (const key of [target, proposal, gaps])
      expect(cache.getQueryState(key)?.isInvalidated).toBe(true);
    cache.clear();
  });
});
