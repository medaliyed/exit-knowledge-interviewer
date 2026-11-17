import { describe, expect, it, vi } from 'vitest';
import { ApiClient, ApiError } from '../api';
import { InterviewDelivery } from './delivery';
import type { Receipt } from '../types';

function deferred() {
  let resolve!: () => void;
  const promise = new Promise<void>(done => {
    resolve = done;
  });
  return { promise, resolve };
}
function setup() {
  const api = new ApiClient(null, vi.fn());
  const drafts = new Map([
    ['answer:session', 'Private answer'],
    ['privacy:session', 'true'],
  ]);
  const notify = vi.fn();
  const refresh = vi.fn().mockResolvedValue(undefined);
  const delivery = new InterviewDelivery({ api, drafts, sessionId: 'session', notify, refresh });
  return { api, drafts, notify, refresh, delivery };
}
const answer = {
  action: 'answer',
  question_turn: 1,
  text: 'Private answer',
  confidential: true,
} as const;
const receipt: Receipt = {
  id: 'saved',
  turn: 2,
  status: 'saved',
  corrects: null,
  question_turn: 1,
  result: null,
};

describe('interview delivery and recovery', () => {
  it('clears accepted confidential drafts immediately and locks until processing and refresh settle', async () => {
    const { api, drafts, refresh, delivery } = setup();
    const reply = deferred();
    const reloaded = deferred();
    refresh.mockReturnValue(reloaded.promise);
    const stream = vi
      .spyOn(api, 'stream')
      .mockImplementation(async (_path, _body, _event, accepted) => {
        accepted?.('saved');
        await reply.promise;
      });
    const saved = vi.fn();
    const pending = delivery.send(answer, saved);
    expect(drafts.has('answer:session')).toBe(false);
    expect(drafts.has('privacy:session')).toBe(false);
    expect(saved).toHaveBeenCalledOnce();
    expect(delivery.snapshot().busy).toBe(true);
    await delivery.send(answer, saved);
    expect(stream).toHaveBeenCalledOnce();
    reply.resolve();
    await vi.waitFor(() => expect(refresh).toHaveBeenCalledOnce());
    expect(delivery.snapshot().busy).toBe(true);
    reloaded.resolve();
    await pending;
    expect(delivery.snapshot()).toMatchObject({ busy: false, delivery: 'idle' });
  });

  it('keeps uncertain submissions and draft text until an explicit receipt check confirms refusal', async () => {
    const { api, drafts, delivery } = setup();
    vi.spyOn(api, 'stream').mockRejectedValue(new Error('disconnected'));
    const request = vi.spyOn(api, 'request').mockRejectedValue(new ApiError(503, 'unavailable'));
    await delivery.send(answer, vi.fn());
    const id = delivery.snapshot().unknownId;
    expect(id).toBeTruthy();
    expect(delivery.snapshot().delivery).toBe('uncertain');
    expect(drafts.get('answer:session')).toBe('Private answer');
    request.mockRejectedValue(new ApiError(404, 'not accepted'));
    await delivery.checkUnknown(vi.fn());
    expect(delivery.snapshot()).toMatchObject({ unknownId: null, delivery: 'failed' });
    expect(api.messageId('session:answer', answer)).toBe(id);
    expect(drafts.get('answer:session')).toBe('Private answer');
  });

  it('resumes an accepted failed reply without reposting the answer', async () => {
    const { api, drafts, delivery } = setup();
    const stream = vi
      .spyOn(api, 'stream')
      .mockRejectedValueOnce(new ApiError(502, 'paused', 'model_unavailable'));
    vi.spyOn(api, 'request').mockResolvedValue(receipt);
    await delivery.send(answer, vi.fn());
    expect(drafts.size).toBe(0);
    expect(delivery.snapshot().delivery).toBe('saved');
    stream.mockResolvedValue(undefined);
    await delivery.resume(receipt);
    expect(stream.mock.calls[1].slice(0, 2)).toEqual([
      '/sessions/session/messages/saved/resume',
      undefined,
    ]);
    expect(delivery.snapshot().delivery).toBe('idle');
  });

  it('clears only a corrected draft and retains a separate unsent composer answer', async () => {
    const { api, drafts, delivery } = setup();
    drafts.set('correction:session:2', 'Corrected text');
    vi.spyOn(api, 'stream').mockImplementation(async (_path, _body, _event, accepted) => {
      accepted?.('saved');
    });
    await delivery.send({ action: 'correct', corrects: 2, text: 'Corrected text' }, vi.fn());
    expect(drafts.has('correction:session:2')).toBe(false);
    expect(drafts.get('answer:session')).toBe('Private answer');
    expect(drafts.get('privacy:session')).toBe('true');
  });

  it('clears a late accepted payload after leaving without notifying an inactive page', async () => {
    const { api, drafts, notify, refresh, delivery } = setup();
    const reply = deferred();
    vi.spyOn(api, 'stream').mockImplementation(async (_path, _body, _event, accepted) => {
      await reply.promise;
      accepted?.('saved');
    });
    const pending = delivery.send(answer, vi.fn());
    delivery.deactivate();
    reply.resolve();
    await pending;
    expect(drafts.size).toBe(0);
    expect(notify).not.toHaveBeenCalled();
    expect(refresh).not.toHaveBeenCalled();
  });
});

it('checks an uncertain correction without clearing an unsent composer answer', async () => {
  const { api, drafts, notify, refresh } = setup();
  drafts.set('correction:session:2', 'Corrected text');
  api.messageId('session:correction', { action: 'correct', corrects: 2, text: 'Corrected text' });
  const delivery = new InterviewDelivery({ api, drafts, sessionId: 'session', notify, refresh });
  vi.spyOn(api, 'request').mockResolvedValue({ ...receipt, corrects: 2 });
  const accepted = vi.fn();
  await delivery.checkUnknown(accepted);
  expect(accepted).toHaveBeenCalledWith('correct');
  expect(drafts.has('correction:session:2')).toBe(false);
  expect(drafts.get('answer:session')).toBe('Private answer');
  expect(delivery.snapshot()).toMatchObject({ unknownId: null, delivery: 'saved', busy: false });
});

it('does not probe receipts with credentials rejected by the server', async () => {
  const { api, drafts, delivery } = setup();
  vi.spyOn(api, 'stream').mockRejectedValue(new ApiError(401, 'expired'));
  const request = vi.spyOn(api, 'request');
  await delivery.send(answer, vi.fn());
  expect(request).not.toHaveBeenCalled();
  expect(drafts.get('answer:session')).toBe('Private answer');
});
