import { Button } from '../ui';
import type { Topic } from '../types';
export function Questions({
  topic,
  editable = false,
  busy = false,
  edit,
}: {
  topic: Topic;
  editable?: boolean;
  busy?: boolean;
  edit?: (body: object, success: string) => Promise<boolean>;
}) {
  return (
    <div className="stack topic-questions">
      {topic.questions.length === 0 && (
        <p className="muted">
          {topic.origin === 'expert'
            ? 'The interviewer starts from your note; no questions are drafted in advance.'
            : 'No drafted questions passed the source checks. The interviewer will ask about this topic directly.'}
        </p>
      )}
      {topic.questions.map((question, index) => (
        <div key={index}>
          <p className="claim question-text">
            <strong>Q</strong> {question.text}
          </p>
          {question.origin === 'follow_up' && (
            <>
              <p className="small muted">
                Selected section: <span>{question.section ?? 'new topic'}</span>
              </p>
              {editable && edit && question.gap_id && (
                <form
                  className="stack"
                  onSubmit={event => {
                    event.preventDefault();
                    const text = new FormData(event.currentTarget).get('seed') as string;
                    void edit(
                      { seeds: { [question.gap_id!]: text } },
                      'Follow-up question updated.',
                    );
                  }}
                >
                  <label htmlFor={`seed-${question.gap_id}`}>Edit question {index + 1}</label>
                  <textarea
                    id={`seed-${question.gap_id}`}
                    name="seed"
                    defaultValue={question.text}
                    required
                    maxLength={2000}
                    disabled={busy}
                  />
                  <div className="toolbar">
                    <Button type="submit" disabled={busy}>
                      Save question
                    </Button>
                    <Button
                      disabled={busy}
                      onClick={() =>
                        void edit(
                          { remove_gaps: [question.gap_id] },
                          'Question removed and its gap returned to triage.',
                        )
                      }
                    >
                      Remove question
                    </Button>
                  </div>
                </form>
              )}
            </>
          )}
          {question.quote && (
            <blockquote className="quote">
              <span className="source-label">{question.artifact_id}</span>
              <q>{question.quote}</q>
            </blockquote>
          )}
        </div>
      ))}
      {topic.dropped.length > 0 && (
        <details className="dropped-questions">
          <summary>
            {topic.dropped.length} drafted{' '}
            {topic.dropped.length === 1 ? 'question was' : 'questions were'} left out
          </summary>
          <ul>
            {topic.dropped.map((reason, index) => (
              <li className="muted" key={index}>
                {reason}
              </li>
            ))}
          </ul>
        </details>
      )}
    </div>
  );
}
