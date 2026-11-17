import { Badge, Button } from '../ui';
import type { Plan, Session, Turn } from '../types';
export function correctableTurns(session: Session) {
  const corrected = new Set(
    session.turns.filter(turn => turn.kind === 'correction').map(turn => turn.corrects),
  );
  return session.turns.filter(
    turn =>
      turn.speaker === 'expert' &&
      ['answer', 'correction'].includes(turn.kind) &&
      !corrected.has(turn.ordinal),
  );
}
export function InterviewTranscript({
  session,
  plan,
  blocked,
  onCorrect,
}: {
  session: Session;
  plan?: Plan;
  blocked: boolean;
  onCorrect: (turn: Turn) => void;
}) {
  const correctable = correctableTurns(session);
  const correctedOrdinals = new Set(
    session.turns.filter(turn => turn.kind === 'correction').map(turn => turn.corrects),
  );
  const topicGroups = plan?.topics || [
    { topic_id: session.current_topic_id || '', label: 'Conversation' },
  ];
  return (
    <aside className="interview-transcript" aria-label="Conversation so far">
      {!session.turns.some(turn => turn.ordinal !== session.waiting?.turn) && (
        <div className="transcript-empty">
          <p className="eyebrow">Conversation so far</p>
          <p className="muted">
            Your answers will appear here as the conversation goes on, with what the interviewer
            noted from each one.
          </p>
        </div>
      )}
      {topicGroups.map(group => {
        const turns = session.turns.filter(
          turn => turn.topic_id === group.topic_id && turn.ordinal !== session.waiting?.turn,
        );
        if (!turns.length) return null;
        return (
          <details
            className="transcript"
            key={group.topic_id}
            open={group.topic_id === session.current_topic_id || session.status === 'finished'}
          >
            <summary>
              {group.topic_id === session.current_topic_id
                ? 'Conversation so far · this topic'
                : group.label}
            </summary>
            <ol>
              {turns.map(turn => (
                <li key={turn.ordinal} className={`transcript-turn transcript-${turn.speaker}`}>
                  <p className="eyebrow">
                    {turn.speaker === 'expert' ? 'Your answer' : 'Question'} · turn {turn.ordinal}
                    {turn.section ? ` · ${turn.section}` : ''}
                    {turn.kind === 'correction' ? ` · corrects turn ${turn.corrects}` : ''}
                  </p>
                  <p className={turn.speaker === 'interviewer' ? 'transcript-question' : ''}>
                    {turn.confidential
                      ? '[confidential]'
                      : turn.kind === 'skip'
                        ? 'Skipped by expert.'
                        : turn.text}
                  </p>
                  {correctedOrdinals.has(turn.ordinal) && <Badge>Corrected later</Badge>}
                  {session.notes
                    .filter(note => note.turn === turn.ordinal)
                    .map((note, index) => (
                      <p key={index} className="event">
                        {note.confidential
                          ? `Confidential · ${note.section}: answered confidentially`
                          : `Noted · ${note.section}: ${note.text}`}
                      </p>
                    ))}
                  {session.open_items
                    .filter(item => item.turn === turn.ordinal)
                    .map((item, index) => (
                      <p key={index} className="event">
                        Left open · {item.section}: {item.reason}
                      </p>
                    ))}
                  {correctable.some(item => item.ordinal === turn.ordinal) && (
                    <Button
                      variant="quiet"
                      disabled={blocked}
                      onClick={() => {
                        onCorrect(turn);
                      }}
                    >
                      Correct this answer
                    </Button>
                  )}
                </li>
              ))}
            </ol>
          </details>
        );
      })}
    </aside>
  );
}
