import type { FormEvent, RefObject } from 'react';
import { Button } from '../ui';
import type { Session } from '../types';
export function InterviewComposer({
  waiting,
  answer,
  confidential,
  blocked,
  uncertain,
  composer,
  changeAnswer,
  onTogglePrivacy,
  onSend,
  onSkip,
}: {
  waiting: NonNullable<Session['waiting']>;
  answer: string;
  confidential: boolean;
  blocked: boolean;
  uncertain: boolean;
  composer: RefObject<HTMLTextAreaElement | null>;
  changeAnswer: (value: string) => void;
  onTogglePrivacy: () => void;
  onSend: () => unknown;
  onSkip: () => unknown;
}) {
  return (
    <form
      className="composer stack"
      onSubmit={(event: FormEvent) => {
        event.preventDefault();
        void onSend();
      }}
    >
      <label htmlFor="interview-answer">Your answer</label>
      <textarea
        ref={composer}
        id="interview-answer"
        value={uncertain ? '' : answer}
        onChange={event => changeAnswer(event.target.value)}
        disabled={blocked}
        aria-describedby="answer-help confidentiality-help"
        rows={6}
        required
      />
      <p id="answer-help" className="muted">
        {waiting.section === 'steps'
          ? 'One step per sentence or line helps keep the procedure in order. '
          : 'Answer as you would explain it to a colleague. '}
        “I don’t remember” is a fine answer; it is recorded as left open.
      </p>
      <div className={`privacy-control ${confidential ? 'privacy-on' : ''}`}>
        <button
          className="privacy-switch"
          type="button"
          role="switch"
          aria-checked={confidential}
          aria-describedby="confidentiality-help"
          disabled={blocked}
          onClick={onTogglePrivacy}
        >
          <span className="switch-track" aria-hidden="true" />
          <strong>Confidential</strong>
          <span>· {confidential ? 'on for this answer' : 'off'}</span>
        </button>
        <p id="confidentiality-help" className="muted">
          When on, this answer is stored but never sent to the AI or shown to readers. Its text will
          not be shown again after sending. The runbook says only “answered confidentially”.
        </p>
      </div>
      <div className="row composer-actions">
        <Button type="submit" variant="primary" disabled={blocked || !answer.trim()}>
          {confidential ? 'Send confidentially' : 'Send'} <span aria-hidden="true">→</span>
        </Button>
        <Button disabled={blocked} onClick={() => void onSkip()}>
          Skip this question
        </Button>
      </div>
    </form>
  );
}
