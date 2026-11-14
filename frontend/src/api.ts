import type { StreamEvent } from './types';

export class ApiError extends Error {
  constructor(public status: number, message: string, public code?: string) {
    super(message);
    this.name = 'ApiError';
  }
}

/** The token and logical submission identifiers live only in this tab's memory. */
export class ApiClient {
  private controllers = new Set<AbortController>();
  private responseControllers = new WeakMap<Response, AbortController>();
  private messages = new Map<string, { fingerprint: string; id: string; unconfirmed: boolean }>();
  private destroyed = false;

  constructor(private token: string, private onUnauthorized: () => void) {}

  messageId(scope: string, payload: unknown): string {
    const fingerprint = JSON.stringify(payload);
    const previous = this.messages.get(scope);
    if (previous?.fingerprint === fingerprint) {
      previous.unconfirmed = true;
      return previous.id;
    }
    const id = crypto.randomUUID();
    this.messages.set(scope, { fingerprint, id, unconfirmed: true });
    return id;
  }

  forgetMessage(scope: string) { this.messages.delete(scope); }

  unconfirmedMessageId(scope: string): string | null {
    const submission = this.messages.get(scope);
    return submission?.unconfirmed ? submission.id : null;
  }

  notAccepted(scope: string, id: string) {
    const submission = this.messages.get(scope);
    if (submission?.id === id) submission.unconfirmed = false;
  }

  destroy() {
    this.destroyed = true;
    this.token = '';
    this.messages.clear();
    for (const controller of this.controllers) controller.abort();
    this.controllers.clear();
  }

  private release(response: Response) {
    const controller = this.responseControllers.get(response);
    if (controller) this.controllers.delete(controller);
    this.responseControllers.delete(response);
  }

  async response(path: string, options: RequestInit = {}): Promise<Response> {
    if (this.destroyed) throw new Error('The workspace session changed. Sign in again.');
    const controller = new AbortController();
    this.controllers.add(controller);
    const headers = new Headers(options.headers);
    headers.set('Authorization', `Bearer ${this.token}`);
    if (options.body !== undefined) headers.set('Content-Type', 'application/json');
    try {
      const response = await fetch(path, { ...options, headers, signal: controller.signal, cache: 'no-store' });
      if (this.destroyed) throw new Error('The workspace session changed. Sign in again.');
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        if (response.status === 401) this.onUnauthorized();
        throw new ApiError(response.status, typeof body.detail === 'string' ? body.detail : `The request could not finish (${response.status}).`, body.code);
      }
      this.responseControllers.set(response, controller);
      return response;
    } catch (error) {
      this.controllers.delete(controller);
      throw error;
    }
  }

  async request<T>(path: string, method = 'GET', body?: unknown): Promise<T> {
    const response = await this.response(path, { method, ...(body === undefined ? {} : { body: JSON.stringify(body) }) });
    try {
      const value = await response.json();
      if (this.destroyed) throw new Error('The workspace session changed. Sign in again.');
      return value as T;
    } finally { this.release(response); }
  }

  async download(path: string, filename: string): Promise<void> {
    const response = await this.response(path);
    try {
      const blob = await response.blob();
      if (this.destroyed) throw new Error('The workspace session changed. Sign in again.');
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement('a');
      anchor.href = url; anchor.download = filename;
      document.body.append(anchor); anchor.click(); anchor.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } finally { this.release(response); }
  }

  async stream(path: string, body?: unknown, onEvent?: (event: StreamEvent) => void, onAccepted?: (id: string) => void): Promise<void> {
    const response = await this.response(path, { method: 'POST', ...(body === undefined ? {} : { body: JSON.stringify(body) }) });
    let reader: ReadableStreamDefaultReader<Uint8Array> | undefined;
    try {
      const id = response.headers.get('X-EKI-Message-ID');
      let acceptanceAnnounced = Boolean(id);
      if (id) onAccepted?.(id);
      if (!response.body) throw new Error('The connection ended. Check your saved answer before continuing.');
      reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      let terminal = false;
      const parse = (block: string) => {
        const data = block.split('\n').filter(line => line.startsWith('data:')).map(line => line.slice(5).replace(/^ /, '')).join('\n');
        if (!data) return;
        const event = JSON.parse(data) as StreamEvent;
        if (event.type === 'delivery' && event.id && !acceptanceAnnounced) {
          acceptanceAnnounced = true;
          onAccepted?.(event.id);
        }
        if (event.type === 'error') throw new ApiError(event.code === 'audit_unavailable' ? 503 : 502, event.message || 'Processing paused. Continue the saved answer.', event.code);
        if (event.type === 'question' || event.type === 'done') terminal = true;
        onEvent?.(event);
      };
      while (true) {
        const { value, done } = await reader.read();
        if (this.destroyed) throw new Error('The workspace session changed. Sign in again.');
        buffer += decoder.decode(value || new Uint8Array(), { stream: !done }).replace(/\r/g, '');
        let end: number;
        while ((end = buffer.indexOf('\n\n')) !== -1) {
          parse(buffer.slice(0, end));
          buffer = buffer.slice(end + 2);
        }
        if (done) break;
      }
      if (!terminal) throw new Error('The connection ended before the next question arrived. Check your saved answer, then continue.');
    } finally {
      await reader?.cancel().catch(() => undefined);
      reader?.releaseLock();
      this.release(response);
    }
  }
}
