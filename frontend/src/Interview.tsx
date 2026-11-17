import { useInterviewSession } from './interview/useInterviewSession';
import { useInterviewDelivery } from './interview/useInterviewDelivery';
import type { AcceptedAction } from './interview/delivery';
import { InterviewTopic } from './interview/InterviewTopic';
import { InterviewComposer } from './interview/InterviewComposer';
import { InterviewTranscript, correctableTurns } from './interview/InterviewTranscript';
import { useEffect, useRef, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { useWorkspace } from './workspace';
import { Badge, Button, Empty, Loading, Modal, Notice } from './ui';
import { type Turn } from './types';

export function Interview() {
  const { sessionId = '' } = useParams();
  return <InterviewSession key={sessionId} />;
}

function InterviewSession() {
  const { sessionId = '' } = useParams();
  const { run, identity, drafts } = useWorkspace();
  const { sessionQuery, receiptsQuery, session, planQuery } = useInterviewSession(sessionId);
  const answerKey = `answer:${sessionId}`;
  const privacyKey = `privacy:${sessionId}`;
  const [answer, setAnswer] = useState(() => drafts.get(answerKey) || '');
  const [confidential, setConfidential] = useState(() => drafts.get(privacyKey) === 'true');
  const [correctionOpen, setCorrectionOpen] = useState(false);
  const [stopOpen, setStopOpen] = useState(false);
  const [selectedTurn, setSelectedTurn] = useState('');
  const [correction, setCorrection] = useState('');
  const composer = useRef<HTMLTextAreaElement>(null);
  const { controller, delivery, failure, events, unknownId, busy } =
    useInterviewDelivery(sessionId);
  useEffect(() => {
    if (session?.waiting && !busy) composer.current?.focus();
  }, [session?.waiting?.turn, busy]);
  function accepted(action: AcceptedAction) {
    if (action === 'correct') {
      setCorrection('');
      setCorrectionOpen(false);
    } else {
      setAnswer('');
      setConfidential(false);
    }
  }
  async function send(action: 'answer' | 'skip' | 'correct', turn?: Turn) {
    if (!session) return;
    if (action === 'correct') {
      if (turn)
        await controller.send({ action, corrects: turn.ordinal, text: correction }, accepted);
    } else if (session.waiting) {
      await controller.send(
        {
          action,
          question_turn: session.waiting.turn,
          text: action === 'skip' ? '' : answer,
          confidential: action === 'answer' && confidential,
        },
        accepted,
      );
    }
  }
  const resume = controller.resume;
  const checkUnknown = () => controller.checkUnknown(accepted);
  const stop = () =>
    controller.stop(() => {
      setAnswer('');
      setConfidential(false);
      setStopOpen(false);
    });

  if (sessionQuery.isPending || receiptsQuery.isPending) return <Loading />;
  if (sessionQuery.error || receiptsQuery.error)
    return (
      <Empty title="The conversation could not be opened">
        <p>Saved answers remain on the server. Try opening the conversation again.</p>
        <Button
          onClick={() => {
            void sessionQuery.refetch();
            void receiptsQuery.refetch();
          }}
        >
          Try again
        </Button>
      </Empty>
    );
  if (!session || !run)
    return (
      <Empty title="Conversation unavailable">
        <p>Select a handover to continue.</p>
      </Empty>
    );
  if (session.run_id !== run.id)
    return (
      <Empty title="This conversation belongs to another handover">
        <p>Open a conversation from this handover’s interview plans.</p>
        <Link className="btn" to={`/workspace/${run.id}/plans`}>
          Interview plans
        </Link>
      </Empty>
    );
  const plan = planQuery.data;
  const pending = receiptsQuery.data || [];
  // Acceptance clears the payload before the stream has finished processing it.
  // Keep the composer locked until the entire request and its refresh settle.
  const locked = busy || ['sending', 'processing'].includes(delivery);
  const followUp = session.kind === 'follow_up';
  const unavailable =
    followUp &&
    (run.owner_unavailable ||
      identity.name !== run.expert_name ||
      (run.owner_subject !== undefined && run.owner_subject !== identity.subject));
  const blocked =
    locked ||
    sessionQuery.isFetching ||
    receiptsQuery.isFetching ||
    pending.length > 0 ||
    Boolean(unknownId) ||
    unavailable;
  const correctable = correctableTurns(session);
  const selected = correctable.find(turn => String(turn.ordinal) === selectedTurn);
  const base = `/workspace/${run.id}`;
  const notedCount = session.notes.filter(note => !note.confidential).length;
  const openCount = session.open_items.length;

  const changeAnswer = (value: string) => {
    setAnswer(value);
    drafts.set(answerKey, value);
  };
  const chooseCorrection = (value: string) => {
    setSelectedTurn(value);
    const turn = correctable.find(item => String(item.ordinal) === value);
    setCorrection(
      drafts.get(`correction:${sessionId}:${value}`) ||
        (turn?.confidential ? '' : turn?.text || ''),
    );
  };

  if (run.maintenance_started_at)
    return (
      <div className="page-content stack">
        <h1>Capture is closed</h1>
        <Notice>
          This handover is in maintenance. The saved interview remains available as source history;
          the current owner maintains its entries.
        </Notice>
        <Link className="btn" to={`${base}/maintenance`}>
          Open maintenance
        </Link>
      </div>
    );
  return (
    <div className="interview-view">
      <div className="interview-layout">
        <InterviewTopic
          session={session}
          plan={plan}
          topicError={planQuery.error}
          onReloadTopics={() => planQuery.refetch()}
          blocked={blocked}
          onStop={() => setStopOpen(true)}
        />

        <section className="interview-main" aria-label="Interview question and answer">
          {unavailable && (
            <Notice error>
              The original expert must be signed in as the current owner to answer this follow-up.
              Their account must be available.
            </Notice>
          )}
          {(delivery === 'saved' || delivery === 'processing') && (
            <Notice>
              <strong>Your answer is saved.</strong>
              {locked && (
                <p>The interviewer is reading your answer. This may take a little while.</p>
              )}
            </Notice>
          )}
          {delivery === 'sending' && (
            <Notice>
              <strong>Sending your answer…</strong>
              <p>Keep this page open until it is saved.</p>
            </Notice>
          )}
          {failure && (
            <Notice error>
              <p>{failure}</p>
              {unknownId && (
                <Button onClick={() => void checkUnknown()} disabled={locked}>
                  Check saved answer
                </Button>
              )}
            </Notice>
          )}
          {unknownId && !failure && (
            <Notice>
              <p>
                A previous submission needs its saved state checked before you continue. Its text is
                hidden until the check finishes.
              </p>
              <Button disabled={locked} onClick={() => void checkUnknown()}>
                Check saved answer
              </Button>
            </Notice>
          )}
          {pending.map(receipt => (
            <Notice key={receipt.id}>
              <strong>Your answer is saved.</strong>
              <p>
                {receipt.status === 'processing'
                  ? 'Processing was interrupted or is still continuing. You can safely check and continue.'
                  : 'The next question did not arrive. Continue when you are ready.'}
              </p>
              <Button variant="primary" disabled={locked} onClick={() => void resume(receipt)}>
                Continue
              </Button>
            </Notice>
          ))}
          {events.length > 0 && (
            <ul className="event-list" aria-live="polite" aria-relevant="additions">
              {events.map((text, index) => (
                <li key={index}>{text}</li>
              ))}
            </ul>
          )}

          {session.status === 'finished' ? (
            <section className="sheet stack">
              <Badge status="approved">Interview finished</Badge>
              <h1>Thank you, {identity.name.split(' ')[0]}.</h1>
              <p>
                {notedCount} sections noted, {openCount} left open.
              </p>
              {followUp ? (
                <>
                  <p>
                    <strong>
                      {
                        {
                          complete: 'Selected questions attempted',
                          question_limit: 'Question limit reached',
                          model_budget: 'Model budget reached',
                          expert_stopped: 'Stopped by expert',
                        }[session.completion_reason ?? 'complete']
                      }
                    </strong>
                  </p>
                  <p>
                    Captured public answers become unpublished proposals or new topic drafts.
                    Unknown, confidential, skipped and unanswered questions remain unresolved. Apply
                    selected changes, then approve the updated entry separately.
                  </p>
                  <Link className="btn btn-primary" to={`${base}/follow-ups/${sessionId}`}>
                    Review follow-up proposals <span aria-hidden="true">→</span>
                  </Link>
                </>
              ) : (
                <>
                  <p>
                    Each topic is now a draft entry for you to read, correct and approve. Nothing is
                    published without your approval.
                  </p>
                  <Link className="btn btn-primary" to={`${base}/review?session=${sessionId}`}>
                    Review entries <span aria-hidden="true">→</span>
                  </Link>
                  <Link to={`${base}/follow-ups`}>Plan a follow-up</Link>
                </>
              )}
            </section>
          ) : session.waiting ? (
            <>
              <section className="question">
                <p className="eyebrow">
                  Turn {session.waiting.turn} · {session.waiting.section}
                </p>
                <h1>{session.waiting.question}</h1>
              </section>
              <InterviewComposer
                waiting={session.waiting}
                answer={answer}
                confidential={confidential}
                blocked={blocked}
                uncertain={Boolean(unknownId)}
                composer={composer}
                changeAnswer={changeAnswer}
                onTogglePrivacy={() => {
                  setConfidential(!confidential);
                  drafts.set(privacyKey, String(!confidential));
                }}
                onSend={() => send('answer')}
                onSkip={() => send('skip')}
              />
            </>
          ) : (
            !pending.length && (
              <section className="sheet stack">
                <h1>Continue your conversation</h1>
                <p>
                  A saved step needs to finish before the next question appears. Your accepted
                  answers are kept.
                </p>
                <Button variant="primary" disabled={locked} onClick={() => void resume()}>
                  Continue conversation
                </Button>
              </section>
            )
          )}
          {correctable.length > 0 && (
            <Button
              variant="quiet"
              disabled={blocked}
              onClick={() => {
                setCorrectionOpen(true);
                if (!selectedTurn)
                  chooseCorrection(String(correctable[correctable.length - 1].ordinal));
              }}
            >
              Correct an earlier answer
            </Button>
          )}
        </section>

        <InterviewTranscript
          session={session}
          plan={plan}
          blocked={blocked}
          onCorrect={turn => {
            chooseCorrection(String(turn.ordinal));
            setCorrectionOpen(true);
          }}
        />
      </div>

      <Modal
        title="Correct an earlier answer"
        open={correctionOpen}
        onClose={() => {
          if (!locked) setCorrectionOpen(false);
        }}
      >
        <form
          className="stack"
          onSubmit={event => {
            event.preventDefault();
            if (selected) void send('correct', selected);
          }}
        >
          <label htmlFor="correction-turn">Choose your answer</label>
          <select
            id="correction-turn"
            value={selectedTurn}
            disabled={blocked}
            onChange={event => chooseCorrection(event.target.value)}
          >
            {correctable.map(turn => (
              <option key={turn.ordinal} value={turn.ordinal}>
                Turn {turn.ordinal}
                {turn.confidential ? ' · confidential' : ` · ${turn.text.slice(0, 65)}`}
              </option>
            ))}
          </select>
          {selected && (
            <>
              <div className="correction-grid">
                <div>
                  <p className="label">What you said</p>
                  <blockquote>
                    {selected.confidential ? '[confidential]' : selected.text}
                  </blockquote>
                </div>
                <div>
                  <label htmlFor="corrected-answer">Corrected answer</label>
                  <textarea
                    id="corrected-answer"
                    value={correction}
                    required
                    rows={5}
                    disabled={blocked}
                    onChange={event => {
                      setCorrection(event.target.value);
                      drafts.set(`correction:${sessionId}:${selected.ordinal}`, event.target.value);
                    }}
                  />
                </div>
              </div>
              <p className="muted">
                Both versions are kept. Entries that cite this answer return to draft and are
                unavailable to readers until you approve them again.
                {selected.confidential
                  ? ' This correction stays confidential and is never sent to the AI.'
                  : ''}
              </p>
              <div className="row">
                <Button variant="primary" type="submit" disabled={blocked || !correction.trim()}>
                  Save correction
                </Button>
                <Button disabled={locked} onClick={() => setCorrectionOpen(false)}>
                  Cancel
                </Button>
              </div>
            </>
          )}
        </form>
      </Modal>
      <Modal
        title="Stop this follow-up?"
        open={stopOpen}
        onClose={() => {
          if (!locked) setStopOpen(false);
        }}
      >
        <p>
          Keep the answers already captured and finish this follow-up without more questions.
          Unanswered gaps return to triage.
        </p>
        {answer.trim() && (
          <Notice error>
            Your unsent answer will be discarded. Cancel and send it first if you want to keep it.
          </Notice>
        )}
        <div className="toolbar">
          <Button variant="primary" disabled={blocked} onClick={() => void stop()}>
            Stop and keep captured answers
          </Button>
          <Button disabled={locked} onClick={() => setStopOpen(false)}>
            Keep interviewing
          </Button>
        </div>
      </Modal>
    </div>
  );
}
