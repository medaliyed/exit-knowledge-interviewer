import { ApiClient, ApiError } from '../api';
import type { Receipt, StreamEvent } from '../types';

export type Submission =
  | { action: 'correct'; corrects: number; text: string }
  | { action: 'answer' | 'skip'; question_turn: number; text: string; confidential: boolean };
export type AcceptedAction = 'answer' | 'correct';
type Delivery = 'idle' | 'sending' | 'saved' | 'processing' | 'uncertain' | 'failed';
export interface DeliveryState {
  delivery: Delivery;
  failure: string;
  events: string[];
  unknownId: string | null;
  busy: boolean;
}
interface Options {
  api: ApiClient;
  sessionId: string;
  drafts: Map<string, string>;
  notify: (message: string) => void;
  refresh: () => Promise<unknown>;
}
function describeEvent(event: StreamEvent): string | null {
  if (event.type === 'note')
    return event.confidential
      ? `Confidential · ${event.section}: answered confidentially`
      : `Noted · ${event.section}: ${event.text || ''}`;
  if (event.type === 'open')
    return `Left open · ${event.section}: ${event.reason || event.text || ''}`;
  if (event.type === 'corrected')
    return 'Answer corrected. Related entries are open for review again.';
  if (event.type === 'recovered') return 'Continuing your saved conversation…';
  if (event.type === 'topic') return `Moving to ${event.label || 'the next topic'}.`;
  return null;
}
function failureText(error: unknown, accepted: boolean) {
  if (error instanceof ApiError && error.code === 'audit_unavailable')
    return 'Processing paused because the activity record could not be saved. Your accepted answer is kept. Continue when the service is available.';
  if (error instanceof ApiError && error.code === 'model_unavailable')
    return 'The interviewer cannot be reached right now. Your answer is saved. Continue when you are ready.';
  if (accepted)
    return 'Your answer is saved. The next question did not arrive. Continue when you are ready.';
  return error instanceof Error
    ? error.message
    : 'The request could not finish. Your typed answer is still here.';
}

/** Session-owned delivery rules; React subscribes without owning in-flight submissions. */
export class InterviewDelivery {
  private state: DeliveryState;
  private listeners = new Set<() => void>();
  private active = true;
  constructor(private options: Options) {
    const { api, sessionId } = options;
    const unknownId =
      api.unconfirmedMessageId(`${sessionId}:answer`) ||
      api.unconfirmedMessageId(`${sessionId}:correction`);
    this.state = {
      delivery: unknownId ? 'uncertain' : 'idle',
      failure: '',
      events: [],
      unknownId,
      busy: false,
    };
  }
  snapshot = () => this.state;
  subscribe = (listener: () => void) => {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  };
  activate = () => {
    this.active = true;
  };
  deactivate = () => {
    this.active = false;
  };
  private update(change: Partial<DeliveryState>) {
    if (!this.active) return;
    this.state = { ...this.state, ...change };
    for (const listener of this.listeners) listener();
  }
  private recordEvent = (event: StreamEvent) => {
    const description = describeEvent(event);
    this.update({
      ...(event.type === 'delivery'
        ? { delivery: event.status === 'processing' ? 'processing' : 'saved' }
        : {}),
      ...(description ? { events: [...this.state.events, description] } : {}),
    });
  };
  private clearAccepted(action: AcceptedAction, turn?: number) {
    const { drafts, sessionId } = this.options;
    if (action === 'correct') drafts.delete(`correction:${sessionId}:${turn}`);
    else {
      drafts.delete(`answer:${sessionId}`);
      drafts.delete(`privacy:${sessionId}`);
    }
  }
  private async finish() {
    try {
      if (this.active) await this.options.refresh();
    } finally {
      this.update({ busy: false });
    }
  }
  send = async (payload: Submission, onAccepted: (action: AcceptedAction) => void) => {
    if (this.state.busy || this.state.unknownId) return;
    if (payload.action !== 'skip' && !payload.text.trim()) {
      this.update({ failure: 'Write an answer before sending.' });
      return;
    }
    const { api, sessionId, notify } = this.options;
    const action = payload.action === 'correct' ? 'correct' : 'answer';
    const scope = `${sessionId}:${action === 'correct' ? 'correction' : 'answer'}`;
    const id = api.messageId(scope, payload);
    let accepted = false;
    const markSaved = () => {
      accepted = true;
      api.forgetMessage(scope);
      this.clearAccepted(action, payload.action === 'correct' ? payload.corrects : undefined);
      if (this.active) onAccepted(action);
      this.update({ delivery: 'saved', unknownId: null });
    };
    this.update({ busy: true, delivery: 'sending', failure: '', events: [] });
    try {
      await api.stream(
        `/sessions/${sessionId}/messages`,
        { message_id: id, ...payload },
        this.recordEvent,
        markSaved,
      );
      if (!accepted) markSaved();
      this.update({ delivery: 'idle' });
      if (this.active)
        notify(
          payload.action === 'correct'
            ? 'Correction saved. Related entries need review again.'
            : payload.action === 'skip'
              ? 'Question skipped. This section is left open.'
              : 'Answer saved.',
        );
    } catch (error) {
      if (!(error instanceof ApiError && error.status === 401)) {
        try {
          const receipt = await api.request<Receipt>(`/sessions/${sessionId}/messages/${id}`);
          markSaved();
          this.update({ delivery: receipt.status === 'processed' ? 'idle' : 'saved' });
        } catch (checkError) {
          if (!accepted) {
            const absent = checkError instanceof ApiError && checkError.status === 404;
            if (absent) api.notAccepted(scope, id);
            this.update({
              unknownId: absent ? null : id,
              delivery: absent ? 'failed' : 'uncertain',
            });
          }
        }
        this.update({ failure: failureText(error, accepted) });
      }
    } finally {
      await this.finish();
    }
  };
  resume = async (receipt?: Receipt) => {
    if (this.state.busy) return;
    const { api, sessionId, notify } = this.options;
    this.update({ busy: true, delivery: 'processing', failure: '', events: [] });
    try {
      await api.stream(
        receipt
          ? `/sessions/${sessionId}/messages/${receipt.id}/resume`
          : `/sessions/${sessionId}/resume`,
        undefined,
        this.recordEvent,
      );
      this.update({ delivery: 'idle' });
      if (this.active) notify('Conversation continued.');
    } catch (error) {
      this.update({
        delivery: receipt ? 'saved' : 'failed',
        failure: failureText(error, Boolean(receipt)),
      });
    } finally {
      await this.finish();
    }
  };
  checkUnknown = async (onAccepted: (action: AcceptedAction) => void) => {
    const id = this.state.unknownId;
    if (!id || this.state.busy) return;
    const { api, sessionId } = this.options;
    this.update({ busy: true, delivery: 'processing' });
    try {
      const receipt = await api.request<Receipt>(`/sessions/${sessionId}/messages/${id}`);
      const action = receipt.corrects ? 'correct' : 'answer';
      this.clearAccepted(action, receipt.corrects ?? undefined);
      api.forgetMessage(`${sessionId}:${action === 'correct' ? 'correction' : 'answer'}`);
      if (this.active) onAccepted(action);
      this.update({
        unknownId: null,
        delivery: receipt.status === 'processed' ? 'idle' : 'saved',
        failure: 'Your answer is saved. Continue the conversation when you are ready.',
      });
    } catch (error) {
      if (error instanceof ApiError && error.status === 404) {
        api.notAccepted(`${sessionId}:answer`, id);
        api.notAccepted(`${sessionId}:correction`, id);
        this.update({
          unknownId: null,
          delivery: 'failed',
          failure:
            'The answer was not accepted. Your text is still here; send it again when you are ready.',
        });
      } else
        this.update({
          delivery: 'uncertain',
          failure:
            'We could not check whether your answer was saved. Check again before sending another answer.',
        });
    } finally {
      await this.finish();
    }
  };
  stop = async (onStopped: () => void) => {
    if (this.state.busy) return;
    this.update({ busy: true, delivery: 'processing', failure: '' });
    const { api, sessionId, notify } = this.options;
    try {
      await api.request(`/sessions/${sessionId}/stop`, 'POST');
      this.clearAccepted('answer');
      if (this.active) {
        onStopped();
        notify('Follow-up stopped. Captured answers are kept; unanswered questions remain open.');
      }
      this.update({ delivery: 'idle' });
    } catch (error) {
      this.update({ delivery: 'failed', failure: failureText(error, false) });
    } finally {
      await this.finish();
    }
  };
}
