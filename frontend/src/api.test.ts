import { afterEach, describe, expect, it, vi } from 'vitest';
import { ApiClient, ApiError } from './api';

function eventStream(chunks: string[], messageId?: string) {
  const encoder = new TextEncoder();
  return new Response(new ReadableStream({ start(controller) {
    for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
    controller.close();
  } }), { headers: { 'Content-Type': 'text/event-stream', ...(messageId ? { 'X-EKI-Message-ID': messageId } : {}) } });
}

afterEach(() => vi.unstubAllGlobals());

describe('authenticated API transport', () => {
  it('reassembles fragmented SSE and announces acceptance before processing', async () => {
    const fetch = vi.fn().mockResolvedValue(eventStream([
      ': ping\r\n\r\n', 'event: delivery\r\ndata: {"type":"del',
      'ivery","status":"saved","id":"stable"}\r\n\r',
      '\nevent: note\r\ndata: {"type":"note","section":"steps","text":"Restart once."}\r\n\r\n',
      'event: question\r\ndata: {"type":"question","turn":3}\r\n\r\n',
    ], 'stable'));
    vi.stubGlobal('fetch', fetch);
    const client = new ApiClient('test-token', vi.fn());
    const calls: string[] = [];
    await client.stream('/sessions/example/messages', { text: 'Restart once.' }, event => calls.push(event.type), () => calls.push('accepted'));
    expect(calls).toEqual(['accepted', 'delivery', 'note', 'question']);
    expect(fetch.mock.calls[0][1].method).toBe('POST');
    expect(new Headers(fetch.mock.calls[0][1].headers).get('Authorization')).toBe('Bearer test-token');
  });

  it('never treats a saved answer without a terminal event as completed', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(eventStream(['data: {"type":"delivery","status":"saved"}\n\n'])));
    await expect(new ApiClient('t', vi.fn()).stream('/sessions/example/resume')).rejects.toThrow(/connection ended/i);
  });

  it('recognizes acceptance from delivery when an intermediary removes the message header', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(eventStream(['data: {"type":"delivery","id":"saved","status":"saved"}\n\ndata: {"type":"done"}\n\n'])));
    const accepted = vi.fn();
    await new ApiClient('t', vi.fn()).stream('/sessions/example/messages', { confidential: true, text: 'Private answer' }, undefined, accepted);
    expect(accepted).toHaveBeenCalledExactlyOnceWith('saved');
  });

  it('retains the server failure code for a paused model or audit step', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(eventStream(['data: {"type":"error","code":"audit_unavailable","message":"Processing paused."}\n\n'])));
    await expect(new ApiClient('t', vi.fn()).stream('/sessions/example/resume')).rejects.toMatchObject({ code: 'audit_unavailable', status: 503 });
  });

  it('changes logical submission identity only when the payload changes', () => {
    const client = new ApiClient('t', vi.fn());
    const payload = { action: 'answer', question_turn: 2, text: 'Restart once.', confidential: false };
    const id = client.messageId('session', payload);
    expect(client.messageId('session', { ...payload })).toBe(id);
    expect(client.messageId('session', { ...payload, text: 'Restart twice.' })).not.toBe(id);
    expect(client.messageId('other-session', payload)).not.toBe(id);
  });

  it('keeps uncertain acceptance across page navigation, while retaining an ID after a confirmed refusal', () => {
    const client = new ApiClient('t', vi.fn());
    const payload = { action: 'answer', question_turn: 2, text: 'Private answer', confidential: true };
    const id = client.messageId('session:answer', payload);
    expect(client.unconfirmedMessageId('session:answer')).toBe(id);
    client.notAccepted('session:answer', id);
    expect(client.unconfirmedMessageId('session:answer')).toBeNull();
    expect(client.messageId('session:answer', payload)).toBe(id);
    expect(client.unconfirmedMessageId('session:answer')).toBe(id);
    client.forgetMessage('session:answer');
    expect(client.unconfirmedMessageId('session:answer')).toBeNull();
  });

  it('invalidates the authenticated workspace on 401 without retrying', async () => {
    const fetch = vi.fn().mockResolvedValue(new Response('{"detail":"invalid token"}', { status: 401 }));
    vi.stubGlobal('fetch', fetch);
    const unauthorized = vi.fn();
    await expect(new ApiClient('t', unauthorized).request('/runs')).rejects.toBeInstanceOf(ApiError);
    expect(unauthorized).toHaveBeenCalledOnce();
    expect(fetch).toHaveBeenCalledOnce();
  });

  it('aborts outstanding work and refuses requests from a signed-out client', async () => {
    let signal: AbortSignal | undefined;
    vi.stubGlobal('fetch', vi.fn((_path, options) => {
      signal = options.signal;
      return new Promise((_resolve, reject) => signal?.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError'))));
    }));
    const client = new ApiClient('t', vi.fn());
    const pending = client.request('/runs');
    client.destroy();
    await expect(pending).rejects.toThrow();
    expect(signal?.aborted).toBe(true);
    await expect(client.request('/runs')).rejects.toThrow(/session changed/i);
  });
});
