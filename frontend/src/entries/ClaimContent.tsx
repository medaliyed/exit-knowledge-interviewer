import { Source } from '../ui';
import type { Citation } from '../types';
export function ClaimContent({
  text,
  citations,
  unsupported,
  manager,
  openSource,
}: {
  text: string;
  citations: Citation[];
  unsupported?: string[];
  manager: boolean;
  openSource: (citation: Citation) => void;
}) {
  return (
    <>
      <p className="claim preserve-lines">{text}</p>
      <div className="source-list">
        {citations.map((citation, index) => (
          <span key={index}>
            <Source
              citation={citation}
              onClick={
                manager && citation.kind === 'interview_turn'
                  ? () => openSource(citation)
                  : undefined
              }
            />
            {citation.kind === 'artifact' && citation.quote && (
              <q className="small muted">{citation.quote}</q>
            )}
          </span>
        ))}
        {!citations.length && (
          <span className="warning-text small">This statement needs a source before approval.</span>
        )}
      </div>
      {unsupported?.length ? (
        <p className="hint">
          Not in the cited answer: {unsupported.join(', ')}. Check the wording against the cited
          answer.
        </p>
      ) : null}
    </>
  );
}
