import { Badge, Button, Notice } from '../ui';
import { sections, type Plan, type Session } from '../types';
const title = (value: string) => value.charAt(0).toUpperCase() + value.slice(1);
const statusLabel = {
  missing: 'missing',
  noted: 'noted',
  open: 'left open',
  confidential: 'confidential',
};
export function InterviewTopic({
  session,
  plan,
  topicError,
  onReloadTopics,
  blocked,
  onStop,
}: {
  session: Session;
  plan?: Plan;
  topicError: unknown;
  onReloadTopics: () => unknown;
  blocked: boolean;
  onStop: () => void;
}) {
  const followUp = session.kind === 'follow_up';
  const topic = plan?.topics.find(item => item.topic_id === session.current_topic_id);
  const topicIndex =
    plan?.topics.findIndex(item => item.topic_id === session.current_topic_id) ?? -1;
  return (
    <aside className="interview-topic">
      {followUp && <Badge>Focused follow-up</Badge>}
      <p className="eyebrow">
        {session.status === 'finished'
          ? 'Interview finished'
          : topicIndex >= 0
            ? `Topic ${topicIndex + 1} of ${plan?.topics.length}`
            : 'Current topic'}
      </p>
      <h2>
        {topic?.label ||
          (session.status === 'finished' ? 'A lasting reference' : 'Your conversation')}
      </h2>
      {topic && (
        <p className="muted">
          {topic.kind}
          {topic.system ? ` · ${topic.system}` : ''}
        </p>
      )}
      {Boolean(topicError) && (
        <Notice error>
          <p>Topic details could not load. Your conversation is still available.</p>
          <Button onClick={() => void onReloadTopics()}>Reload topics</Button>
        </Notice>
      )}
      <details className="section-checklist" open>
        <summary>Sections of this topic</summary>
        <ul className="checklist">
          {(followUp ? (session.scope ?? topic?.sections ?? sections) : sections).map(name => (
            <li key={name} className={session.waiting?.section === name ? 'now' : ''}>
              <span
                className={`status-mark status-${session.checklist[name] || 'missing'}`}
                aria-hidden="true"
              />
              <span>{title(name)}</span>
              <span className="checklist-state">
                {statusLabel[session.checklist[name] || 'missing']}
                {session.waiting?.section === name ? ' · asking now' : ''}
              </span>
            </li>
          ))}
        </ul>
      </details>
      {followUp && session.usage && (
        <section className="stack small" aria-label="Follow-up usage">
          <h3>Interview limits</h3>
          <p>
            {session.usage.questions} of{' '}
            {session.usage.limits?.questions ?? plan?.limits?.questions ?? '…'} questions used ·{' '}
            {Math.max(
              0,
              (session.usage.limits?.questions ?? plan?.limits?.questions ?? 0) -
                session.usage.questions,
            )}{' '}
            remaining
          </p>
          <p>
            {session.usage.model_calls} of{' '}
            {session.usage.limits?.model_calls ?? plan?.limits?.model_calls ?? '…'} model attempts ·{' '}
            {session.usage.embedding_calls} of{' '}
            {session.usage.limits?.embedding_calls ?? plan?.limits?.embedding_calls ?? '…'}{' '}
            embedding attempts
          </p>
          {session.usage.duration_seconds !== undefined && (
            <p>
              {Math.round(session.usage.duration_seconds)} seconds elapsed
              {session.usage.input_tokens != null &&
                ` · ${session.usage.input_tokens} input tokens`}
              {session.usage.output_tokens != null &&
                ` · ${session.usage.output_tokens} output tokens`}
            </p>
          )}
        </section>
      )}
      {followUp && session.status === 'active' && (
        <Button disabled={blocked} onClick={() => onStop()}>
          Stop for now
        </Button>
      )}
    </aside>
  );
}
