import { useEffect, useRef, useState, type FormEvent } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Link, useParams } from 'react-router-dom';
import { ApiError } from './api';
import { useWorkspace } from './workspace';
import { Badge, Button, Empty, Loading, Modal, Notice } from './ui';
import { sections, type Plan, type Receipt, type Session, type StreamEvent, type Turn } from './types';

const title = (value: string) => value.charAt(0).toUpperCase() + value.slice(1);
const statusLabel = { missing: 'missing', noted: 'noted', open: 'left open', confidential: 'confidential' };
type Delivery = 'idle' | 'sending' | 'saved' | 'processing' | 'uncertain' | 'failed';

function describeEvent(event: StreamEvent): string | null {
  if (event.type === 'note') return event.confidential ? `Confidential · ${event.section}: answered confidentially` : `Noted · ${event.section}: ${event.text || ''}`;
  if (event.type === 'open') return `Left open · ${event.section}: ${event.reason || event.text || ''}`;
  if (event.type === 'corrected') return `Answer corrected. Related entries are open for review again.`;
  if (event.type === 'recovered') return 'Continuing your saved conversation…';
  if (event.type === 'topic') return `Moving to ${event.label || 'the next topic'}.`;
  return null;
}

export function Interview() {
  const { sessionId = '' } = useParams();
  return <InterviewSession key={sessionId} />;
}

function InterviewSession() {
  const { sessionId = '' } = useParams();
  const { api, run, identity, notify, drafts } = useWorkspace();
  const queryClient = useQueryClient();
  const sessionQuery = useQuery({ queryKey: ['session', sessionId], queryFn: () => api.request<Session>(`/sessions/${sessionId}`), retry: false, refetchOnMount: 'always' });
  const receiptsQuery = useQuery({ queryKey: ['receipts', sessionId], queryFn: () => api.request<Receipt[]>(`/sessions/${sessionId}/messages`), retry: false, refetchOnMount: 'always' });
  const session = sessionQuery.data;
  const planQuery = useQuery({ queryKey: ['plan', session?.plan_id], queryFn: () => api.request<Plan>(`/plans/${session!.plan_id}`), enabled: Boolean(session?.plan_id), retry: false });
  const answerKey = `answer:${sessionId}`;
  const privacyKey = `privacy:${sessionId}`;
  const [answer, setAnswer] = useState(() => drafts.get(answerKey) || '');
  const [confidential, setConfidential] = useState(() => drafts.get(privacyKey) === 'true');
  const [delivery, setDelivery] = useState<Delivery>('idle');
  const [failure, setFailure] = useState('');
  const [events, setEvents] = useState<string[]>([]);
  const [unknownId, setUnknownId] = useState<string | null>(() => api.unconfirmedMessageId(`${sessionId}:answer`) || api.unconfirmedMessageId(`${sessionId}:correction`));
  const [correctionOpen, setCorrectionOpen] = useState(false);
  const [selectedTurn, setSelectedTurn] = useState('');
  const [correction, setCorrection] = useState('');
  const composer = useRef<HTMLTextAreaElement>(null);
  const live = useRef(true);
  const busy = useRef(false);
  useEffect(() => { live.current = true; return () => { live.current = false; }; }, []);
  useEffect(() => {
    setAnswer(drafts.get(answerKey) || '');
    setConfidential(drafts.get(privacyKey) === 'true');
    const uncertain = api.unconfirmedMessageId(`${sessionId}:answer`) || api.unconfirmedMessageId(`${sessionId}:correction`);
    setDelivery(uncertain ? 'uncertain' : 'idle'); setFailure(''); setUnknownId(uncertain); setEvents([]);
  }, [answerKey, privacyKey, drafts]);
  useEffect(() => {
    if (session?.waiting && !busy.current) composer.current?.focus();
  }, [session?.waiting?.turn]);

  const reload = async () => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ['session', sessionId] }),
      queryClient.invalidateQueries({ queryKey: ['receipts', sessionId] }),
      queryClient.invalidateQueries({ queryKey: ['entries'] }),
      queryClient.invalidateQueries({ queryKey: ['entry'] }),
      queryClient.invalidateQueries({ queryKey: ['approved-entries'] }),
      queryClient.invalidateQueries({ queryKey: ['plans'] }),
    ]);
  };
  const recordEvent = (event: StreamEvent) => {
    if (!live.current) return;
    if (event.type === 'delivery') setDelivery(event.status === 'processing' ? 'processing' : 'saved');
    const description = describeEvent(event);
    if (description) setEvents(previous => [...previous, description]);
  };
  const failureText = (error: unknown, accepted: boolean) => {
    if (error instanceof ApiError && error.code === 'audit_unavailable') return 'Processing paused because the activity record could not be saved. Your accepted answer is kept. Continue when the service is available.';
    if (error instanceof ApiError && error.code === 'model_unavailable') return 'The interviewer cannot be reached right now. Your answer is saved. Continue when you are ready.';
    if (accepted) return 'Your answer is saved. The next question did not arrive. Continue when you are ready.';
    return error instanceof Error ? error.message : 'The request could not finish. Your typed answer is still here.';
  };

  async function send(action: 'answer' | 'skip' | 'correct', turn?: Turn) {
    if (busy.current || !session) return;
    const text = action === 'correct' ? correction : action === 'skip' ? '' : answer;
    if (action !== 'skip' && !text.trim()) { setFailure('Write an answer before sending.'); return; }
    const payload = action === 'correct'
      ? { action, corrects: turn!.ordinal, text }
      : { action, question_turn: session.waiting!.turn, text, confidential: action === 'answer' && confidential };
    const scope = `${sessionId}:${action === 'correct' ? 'correction' : 'answer'}`;
    const id = api.messageId(scope, payload);
    const body = { message_id: id, ...payload };
    let accepted = false;
    const markSaved = () => {
      accepted = true;
      api.forgetMessage(scope);
      if (action === 'correct') {
        drafts.delete(`correction:${sessionId}:${turn!.ordinal}`);
        if (live.current) { setCorrection(''); setCorrectionOpen(false); }
      } else {
        drafts.delete(answerKey); drafts.delete(privacyKey);
        if (live.current) { setAnswer(''); setConfidential(false); }
      }
      if (live.current) { setDelivery('saved'); setUnknownId(null); }
    };
    busy.current = true;
    setDelivery('sending'); setFailure(''); setEvents([]);
    try {
      await api.stream(`/sessions/${sessionId}/messages`, body, recordEvent, markSaved);
      // A terminal event is authoritative even if a proxy stripped the acceptance header.
      if (!accepted) markSaved();
      if (live.current) { setDelivery('idle'); notify(action === 'correct' ? 'Correction saved. Related entries need review again.' : action === 'skip' ? 'Question skipped. This section is left open.' : 'Answer saved.'); }
    } catch (error) {
      if (!(error instanceof ApiError && error.status === 401)) {
        try {
          const receipt = await api.request<Receipt>(`/sessions/${sessionId}/messages/${id}`);
          markSaved();
          if (live.current) setDelivery(receipt.status === 'processed' ? 'idle' : 'saved');
        } catch (checkError) {
          if (live.current && !accepted) {
            // Unknown acceptance must be checked before a changed payload can be sent.
            const absent = checkError instanceof ApiError && checkError.status === 404;
            if (absent) api.notAccepted(scope, id);
            setUnknownId(absent ? null : id);
            setDelivery(absent ? 'failed' : 'uncertain');
          }
        }
        if (live.current) setFailure(failureText(error, accepted));
      }
    } finally {
      busy.current = false;
      if (live.current) await reload();
    }
  }

  async function resume(receipt?: Receipt) {
    if (busy.current) return;
    busy.current = true; setDelivery('processing'); setFailure(''); setEvents([]);
    try {
      await api.stream(receipt ? `/sessions/${sessionId}/messages/${receipt.id}/resume` : `/sessions/${sessionId}/resume`, undefined, recordEvent);
      if (live.current) { setDelivery('idle'); notify('Conversation continued.'); }
    } catch (error) {
      if (live.current) { setDelivery(receipt ? 'saved' : 'failed'); setFailure(failureText(error, Boolean(receipt))); }
    } finally { busy.current = false; if (live.current) await reload(); }
  }

  async function checkUnknown() {
    if (!unknownId || busy.current) return;
    busy.current = true; setDelivery('processing');
    try {
      const receipt = await api.request<Receipt>(`/sessions/${sessionId}/messages/${unknownId}`);
      // Clear only the accepted payload, before its transcript loads; a separate
      // unsent composer answer must survive correcting an earlier turn.
      if (receipt.corrects) {
        drafts.delete(`correction:${sessionId}:${receipt.corrects}`); api.forgetMessage(`${sessionId}:correction`);
        if (live.current) { setCorrection(''); setCorrectionOpen(false); }
      } else {
        drafts.delete(answerKey); drafts.delete(privacyKey); api.forgetMessage(`${sessionId}:answer`);
        if (live.current) { setAnswer(''); setConfidential(false); }
      }
      if (live.current) {
        setUnknownId(null); setDelivery(receipt.status === 'processed' ? 'idle' : 'saved');
        setFailure('Your answer is saved. Continue the conversation when you are ready.');
      }
    } catch (error) {
      if (error instanceof ApiError && error.status === 404) {
        api.notAccepted(`${sessionId}:answer`, unknownId); api.notAccepted(`${sessionId}:correction`, unknownId);
        if (live.current) { setUnknownId(null); setDelivery('failed'); setFailure('The answer was not accepted. Your text is still here; send it again when you are ready.'); }
      } else if (live.current) { setDelivery('uncertain'); setFailure('We could not check whether your answer was saved. Check again before sending another answer.'); }
    } finally { busy.current = false; if (live.current) await reload(); }
  }

  if (sessionQuery.isPending || receiptsQuery.isPending) return <Loading />;
  if (sessionQuery.error || receiptsQuery.error) return <Empty title="The conversation could not be opened"><p>Saved answers remain on the server. Try opening the conversation again.</p><Button onClick={() => { void sessionQuery.refetch(); void receiptsQuery.refetch(); }}>Try again</Button></Empty>;
  if (!session || !run) return <Empty title="Conversation unavailable"><p>Select a handover to continue.</p></Empty>;
  if (session.run_id !== run.id) return <Empty title="This conversation belongs to another handover"><p>Open a conversation from this handover’s interview plans.</p><Link className="btn" to={`/workspace/${run.id}/plans`}>Interview plans</Link></Empty>;
  const plan = planQuery.data;
  const topic = plan?.topics.find(item => item.topic_id === session.current_topic_id);
  const topicIndex = plan?.topics.findIndex(item => item.topic_id === session.current_topic_id) ?? -1;
  const pending = receiptsQuery.data || [];
  const locked = ['sending', 'processing'].includes(delivery);
  const blocked = locked || sessionQuery.isFetching || receiptsQuery.isFetching || pending.length > 0 || Boolean(unknownId);
  const correctedOrdinals = new Set(session.turns.filter(turn => turn.kind === 'correction').map(turn => turn.corrects));
  const correctable = session.turns.filter(turn => turn.speaker === 'expert' && ['answer', 'correction'].includes(turn.kind) && !correctedOrdinals.has(turn.ordinal));
  const selected = correctable.find(turn => String(turn.ordinal) === selectedTurn);
  const base = `/workspace/${run.id}`;
  const topicGroups = plan?.topics || [{ topic_id: session.current_topic_id || '', label: 'Conversation' }];
  const notedCount = session.notes.filter(note => !note.confidential).length;
  const openCount = session.open_items.length;

  const changeAnswer = (value: string) => { setAnswer(value); drafts.set(answerKey, value); };
  const chooseCorrection = (value: string) => {
    setSelectedTurn(value);
    const turn = correctable.find(item => String(item.ordinal) === value);
    setCorrection(drafts.get(`correction:${sessionId}:${value}`) || (turn?.confidential ? '' : turn?.text || ''));
  };

  if (run.maintenance_started_at) return <div className="page-content stack"><h1>Capture is closed</h1><Notice>This handover is in maintenance. The saved interview remains available as source history; the current owner maintains its entries.</Notice><Link className="btn" to={`${base}/maintenance`}>Open maintenance</Link></div>;
  return <div className="interview-view">
    <div className="interview-layout">
      <aside className="interview-topic">
        <p className="eyebrow">{session.status === 'finished' ? 'Interview finished' : topicIndex >= 0 ? `Topic ${topicIndex + 1} of ${plan?.topics.length}` : 'Current topic'}</p>
        <h2>{topic?.label || (session.status === 'finished' ? 'A lasting reference' : 'Your conversation')}</h2>
        {topic && <p className="muted">{topic.kind}{topic.system ? ` · ${topic.system}` : ''}</p>}
        {planQuery.error && <Notice error><p>Topic details could not load. Your conversation is still available.</p><Button onClick={() => void planQuery.refetch()}>Reload topics</Button></Notice>}
        <details className="section-checklist" open>
          <summary>Sections of this topic</summary>
          <ul className="checklist">{sections.map(name => <li key={name} className={session.waiting?.section === name ? 'now' : ''}>
            <span className={`status-mark status-${session.checklist[name] || 'missing'}`} aria-hidden="true" />
            <span>{title(name)}</span><span className="checklist-state">{statusLabel[session.checklist[name] || 'missing']}{session.waiting?.section === name ? ' · asking now' : ''}</span>
          </li>)}</ul>
        </details>
      </aside>

      <section className="interview-main" aria-label="Interview question and answer">
        {(delivery === 'saved' || delivery === 'processing') && <Notice><strong>Your answer is saved.</strong>{locked && <p>The interviewer is reading your answer. This may take a little while.</p>}</Notice>}
        {delivery === 'sending' && <Notice><strong>Sending your answer…</strong><p>Keep this page open until it is saved.</p></Notice>}
        {failure && <Notice error><p>{failure}</p>{unknownId && <Button onClick={() => void checkUnknown()} disabled={locked}>Check saved answer</Button>}</Notice>}
        {unknownId && !failure && <Notice><p>A previous submission needs its saved state checked before you continue. Its text is hidden until the check finishes.</p><Button disabled={locked} onClick={() => void checkUnknown()}>Check saved answer</Button></Notice>}
        {pending.map(receipt => <Notice key={receipt.id}><strong>Your answer is saved.</strong><p>{receipt.status === 'processing' ? 'Processing was interrupted or is still continuing. You can safely check and continue.' : 'The next question did not arrive. Continue when you are ready.'}</p><Button variant="primary" disabled={locked} onClick={() => void resume(receipt)}>Continue</Button></Notice>)}
        {events.length > 0 && <ul className="event-list" aria-live="polite" aria-relevant="additions">{events.map((text, index) => <li key={index}>{text}</li>)}</ul>}

        {session.status === 'finished' ? <section className="sheet stack">
          <Badge status="approved">Interview finished</Badge><h1>Thank you, {identity.name.split(' ')[0]}.</h1>
          <p>{notedCount} sections noted, {openCount} left open.</p><p>Each topic is now a draft entry for you to read, correct and approve. Nothing is published without your approval.</p>
          <Link className="btn btn-primary" to={`${base}/review?session=${sessionId}`}>Review entries <span aria-hidden="true">→</span></Link>
        </section> : session.waiting ? <>
          <section className="question"><p className="eyebrow">Turn {session.waiting.turn} · {session.waiting.section}</p><h1>{session.waiting.question}</h1></section>
          <form className="composer stack" onSubmit={(event: FormEvent) => { event.preventDefault(); void send('answer'); }}>
            <label htmlFor="interview-answer">Your answer</label>
            <textarea ref={composer} id="interview-answer" value={unknownId ? '' : answer} onChange={event => changeAnswer(event.target.value)} disabled={blocked} aria-describedby="answer-help confidentiality-help" rows={6} required />
            <p id="answer-help" className="muted">{session.waiting.section === 'steps' ? 'One step per sentence or line helps keep the procedure in order. ' : 'Answer as you would explain it to a colleague. '}“I don’t remember” is a fine answer; it is recorded as left open.</p>
            <div className={`privacy-control ${confidential ? 'privacy-on' : ''}`}>
              <button className="privacy-switch" type="button" role="switch" aria-checked={confidential} aria-describedby="confidentiality-help" disabled={blocked} onClick={() => { setConfidential(!confidential); drafts.set(privacyKey, String(!confidential)); }}>
                <span className="switch-track" aria-hidden="true" /><strong>Confidential</strong><span>· {confidential ? 'on for this answer' : 'off'}</span>
              </button>
              <p id="confidentiality-help" className="muted">When on, this answer is stored but never sent to the AI or shown to readers. Its text will not be shown again after sending. The runbook says only “answered confidentially”.</p>
            </div>
            <div className="row composer-actions"><Button type="submit" variant="primary" disabled={blocked || !answer.trim()}>{confidential ? 'Send confidentially' : 'Send'} <span aria-hidden="true">→</span></Button><Button disabled={blocked} onClick={() => void send('skip')}>Skip this question</Button></div>
          </form>
        </> : !pending.length && <section className="sheet stack"><h1>Continue your conversation</h1><p>A saved step needs to finish before the next question appears. Your accepted answers are kept.</p><Button variant="primary" disabled={locked} onClick={() => void resume()}>Continue conversation</Button></section>}
        {correctable.length > 0 && <Button variant="quiet" disabled={blocked} onClick={() => { setCorrectionOpen(true); if (!selectedTurn) chooseCorrection(String(correctable[correctable.length - 1].ordinal)); }}>Correct an earlier answer</Button>}
      </section>

      <aside className="interview-transcript" aria-label="Conversation so far">
        {!session.turns.some(turn => turn.ordinal !== session.waiting?.turn) && <div className="transcript-empty"><p className="eyebrow">Conversation so far</p><p className="muted">Your answers will appear here as the conversation goes on, with what the interviewer noted from each one.</p></div>}
        {topicGroups.map(group => {
          const turns = session.turns.filter(turn => turn.topic_id === group.topic_id && turn.ordinal !== session.waiting?.turn);
          if (!turns.length) return null;
          return <details className="transcript" key={group.topic_id} open={group.topic_id === session.current_topic_id || session.status === 'finished'}>
            <summary>{group.topic_id === session.current_topic_id ? 'Conversation so far · this topic' : group.label}</summary>
            <ol>{turns.map(turn => <li key={turn.ordinal} className={`transcript-turn transcript-${turn.speaker}`}>
              <p className="eyebrow">{turn.speaker === 'expert' ? 'Your answer' : 'Question'} · turn {turn.ordinal}{turn.section ? ` · ${turn.section}` : ''}{turn.kind === 'correction' ? ` · corrects turn ${turn.corrects}` : ''}</p>
              <p className={turn.speaker === 'interviewer' ? 'transcript-question' : ''}>{turn.confidential ? '[confidential]' : turn.kind === 'skip' ? 'Skipped by expert.' : turn.text}</p>
              {correctedOrdinals.has(turn.ordinal) && <Badge>Corrected later</Badge>}
              {session.notes.filter(note => note.turn === turn.ordinal).map((note, index) => <p key={index} className="event">{note.confidential ? `Confidential · ${note.section}: answered confidentially` : `Noted · ${note.section}: ${note.text}`}</p>)}
              {session.open_items.filter(item => item.turn === turn.ordinal).map((item, index) => <p key={index} className="event">Left open · {item.section}: {item.reason}</p>)}
              {correctable.some(item => item.ordinal === turn.ordinal) && <Button variant="quiet" disabled={blocked} onClick={() => { chooseCorrection(String(turn.ordinal)); setCorrectionOpen(true); }}>Correct this answer</Button>}
            </li>)}</ol>
          </details>;
        })}
      </aside>
    </div>

    <Modal title="Correct an earlier answer" open={correctionOpen} onClose={() => { if (!locked) setCorrectionOpen(false); }}>
      <form className="stack" onSubmit={event => { event.preventDefault(); if (selected) void send('correct', selected); }}>
        <label htmlFor="correction-turn">Choose your answer</label><select id="correction-turn" value={selectedTurn} disabled={blocked} onChange={event => chooseCorrection(event.target.value)}>{correctable.map(turn => <option key={turn.ordinal} value={turn.ordinal}>Turn {turn.ordinal}{turn.confidential ? ' · confidential' : ` · ${turn.text.slice(0, 65)}`}</option>)}</select>
        {selected && <><div className="correction-grid"><div><p className="label">What you said</p><blockquote>{selected.confidential ? '[confidential]' : selected.text}</blockquote></div><div><label htmlFor="corrected-answer">Corrected answer</label><textarea id="corrected-answer" value={correction} required rows={5} disabled={blocked} onChange={event => { setCorrection(event.target.value); drafts.set(`correction:${sessionId}:${selected.ordinal}`, event.target.value); }} /></div></div>
          <p className="muted">Both versions are kept. Entries that cite this answer return to draft and are unavailable to readers until you approve them again.{selected.confidential ? ' This correction stays confidential and is never sent to the AI.' : ''}</p>
          <div className="row"><Button variant="primary" type="submit" disabled={blocked || !correction.trim()}>Save correction</Button><Button disabled={locked} onClick={() => setCorrectionOpen(false)}>Cancel</Button></div>
        </>}
      </form>
    </Modal>
  </div>;
}
