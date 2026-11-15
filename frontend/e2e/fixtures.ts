import { expect, type Page, type Route } from '@playwright/test';
import { sections, type Entry, type Plan, type Receipt, type Section, type SectionName, type Session, type StreamEvent } from '../src/types';

export const ids = {
  run: '11111111-1111-4111-8111-111111111111',
  plan: '22222222-2222-4222-8222-222222222222',
  session: '33333333-3333-4333-8333-333333333333',
  entry: '44444444-4444-4444-8444-444444444444',
};
export const workspace = `/workspace/${ids.run}`;
export const expert = 'Nadia Ferrand';
const at = '2026-11-14T10:00:00+00:00';
const cite = { kind: 'interview_turn' as const, session_id: ids.session, turn_index: 2 };
const firstQuestion = 'What starts the month-end reconciliation?';
const nextQuestion = 'How do we roll it back?';

export interface RecordedRequest { method: string; path: string; body: Record<string, unknown> | null }
export interface MockOptions { approvedPlan?: boolean; approvedEntry?: boolean; interruptMessage?: boolean }

/** Stateful synthetic API double: mirrors public backend response schemas only. */
export async function mockWorkspace(page: Page, options: MockOptions = {}) {
  const requests: RecordedRequest[] = [];
  let sessionExists = options.approvedPlan ?? false;
  const plan: Plan = {
    id: ids.plan, run_id: ids.run, status: sessionExists ? 'approved' : 'draft',
    approved_by: sessionExists ? expert : null, approved_at: sessionExists ? at : null,
    topics: [
      {
        topic_id: 'topic:reconciliation', label: 'Month-end reconciliation', kind: 'procedure',
        system: 'ledger', origin: 'ranked', score: 88, reason: 'One expert holds the operational knowledge.',
        artifact_ids: ['TCK-0001'],
        questions: [{ text: firstQuestion, origin: 'drafted', artifact_id: 'TCK-0001', quote: 'Nadia restarted reconciliation after checking the ledger.' }],
        dropped: ['A question without a matching source quotation was left out.'],
      },
      {
        topic_id: 'topic:backup', label: 'Backup ownership', kind: 'decision', system: 'ledger',
        origin: 'contradiction', score: 71, reason: 'Its sources contradict each other.',
        artifact_ids: ['TCK-0002'],
        questions: [{ text: 'Who owns the backup decision?', origin: 'drafted', artifact_id: 'TCK-0002', quote: 'The rotation owner should confirm the backup.' }], dropped: [],
      },
    ],
  };
  const session: Session = {
    id: ids.session, plan_id: ids.plan, run_id: ids.run, status: 'active',
    current_topic_id: 'topic:reconciliation',
    waiting: { turn: 1, question: firstQuestion, section: 'trigger', topic_id: 'topic:reconciliation' },
    checklist: { trigger: 'missing', diagnosis: 'missing', steps: 'missing', rollback: 'missing', contacts: 'missing', pitfalls: 'missing', rationale: 'missing' },
    turns: [{ ordinal: 1, topic_id: 'topic:reconciliation', speaker: 'interviewer', kind: 'question', text: firstQuestion, confidential: false, section: 'trigger', answers: null, corrects: null }],
    notes: [], open_items: [],
  };
  const entry: Entry = {
    id: ids.entry, topic_id: 'topic:reconciliation', title: 'Month-end reconciliation',
    status: options.approvedEntry ? 'approved' : 'draft', version: 1, edited: false,
    approved_by: options.approvedEntry ? expert : null, approved_at: options.approvedEntry ? at : null,
    session_id: ids.session, rejection_reason: null, summary: '', unsupported: [], decisions: [],
    ...Object.fromEntries(sections.map(name => [name, {
      claims: [{ text: name === 'steps' ? 'Stop the billing worker.' : `The ${name} is recorded in the approved handover.`, citations: [cite] }],
      open_question: null,
    }])) as Record<SectionName, Section>,
  };
  let pending: Receipt[] = [];
  const summary = () => ({ id: entry.id, topic_id: entry.topic_id, title: entry.title, status: entry.status, version: entry.version, edited: entry.edited, unsupported_count: 0, claim_count: 7, open_question_count: 0 });
  const json = (route: Route, value: unknown, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(value) });
  const stream = (route: Route, events: Array<Record<string, unknown>>, messageId?: string) => route.fulfill({
    status: 200, contentType: 'text/event-stream',
    headers: messageId ? { 'X-EKI-Message-ID': messageId } : {},
    body: events.map(event => `event: ${event.type}\ndata: ${JSON.stringify(event)}\n\n`).join(''),
  });
  const next = (): StreamEvent => {
    session.waiting = { turn: 3, question: nextQuestion, section: 'rollback', topic_id: 'topic:reconciliation' };
    if (!session.turns.some(turn => turn.ordinal === 3)) session.turns.push({ ordinal: 3, topic_id: 'topic:reconciliation', speaker: 'interviewer', kind: 'question', text: nextQuestion, confidential: false, section: 'rollback', answers: null, corrects: null });
    return { type: 'question', ...session.waiting };
  };

  await page.route('**/*', async route => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    if (!/^\/(runs|plans|sessions|entries|runbook)(\/|$)/.test(path)) return route.continue();
    const method = request.method();
    const body = request.postDataJSON() as Record<string, unknown> | null;
    requests.push({ method, path, body });
    const token = request.headers().authorization;
    const reader = token === 'Bearer reader-token';
    if (token !== 'Bearer owner-token' && !reader) return json(route, { detail: 'valid bearer credentials required' }, 401);
    if (path === '/runbook/identity') return json(route, { subject: reader ? 'reader' : 'owner', name: reader ? 'Ari Martin' : expert, admin: false });
    if (path === '/runs') return json(route, [{ id: ids.run, expert_name: expert, since: '2024-11-01', until: '2026-10-31', status: 'completed', can_manage: !reader }]);
    if (reader && /^\/(plans|sessions)\//.test(path)) return json(route, { detail: 'resource not found' }, 404);
    if (path === `/runs/${ids.run}/plans`) return json(route, [{ id: ids.plan, status: plan.status, session_id: sessionExists ? ids.session : null }]);
    if (path === `/runs/${ids.run}/generations`) return json(route, []);
    if (path === `/plans/${ids.plan}` && method === 'GET') return json(route, plan);
    if (path === `/plans/${ids.plan}` && method === 'PATCH') {
      const order = body?.order as string[] | undefined;
      if (order) plan.topics = order.map(id => plan.topics.find(topic => topic.topic_id === id)!);
      const remove = body?.remove as string[] | undefined;
      if (remove) plan.topics = plan.topics.filter(topic => !remove.includes(topic.topic_id));
      return json(route, plan);
    }
    if (path === `/plans/${ids.plan}/approve`) {
      plan.status = 'approved'; plan.approved_by = expert; plan.approved_at = at; sessionExists = true;
      const first = plan.topics[0];
      session.current_topic_id = first.topic_id;
      session.waiting = { turn: 1, question: first.questions[0].text, section: 'trigger', topic_id: first.topic_id };
      session.turns[0] = { ...session.turns[0], topic_id: first.topic_id, text: first.questions[0].text };
      return json(route, session);
    }
    if (path === `/sessions/${ids.session}`) return json(route, session);
    if (path === `/sessions/${ids.session}/entries`) return json(route, [summary()]);
    if (path === `/sessions/${ids.session}/messages` && method === 'GET') return json(route, pending);
    if (path === `/sessions/${ids.session}/messages` && method === 'POST') {
      const messageId = String(body?.message_id);
      const confidential = body?.confidential === true;
      session.turns.push({ ordinal: 2, topic_id: 'topic:reconciliation', speaker: 'expert', kind: 'answer', text: confidential ? '[confidential]' : String(body?.text), confidential, section: null, answers: 1, corrects: null });
      session.notes.push({ topic_id: 'topic:reconciliation', section: 'trigger', text: confidential ? 'Answered confidentially.' : String(body?.text), turn: 2, confidential });
      session.checklist.trigger = confidential ? 'confidential' : 'noted';
      const receipt: Receipt = { id: messageId, turn: 2, status: options.interruptMessage ? 'needs_retry' : 'processed', question_turn: 1, corrects: null, result: null };
      if (options.interruptMessage) {
        pending = [receipt]; session.waiting = null;
        return stream(route, [{ type: 'delivery', ...receipt }], messageId);
      }
      return stream(route, [{ type: 'delivery', ...receipt }, { type: 'note', topic_id: 'topic:reconciliation', section: 'trigger', text: confidential ? 'Answered confidentially.' : String(body?.text), confidential }, next() as unknown as Record<string, unknown>], messageId);
    }
    if (path.startsWith(`/sessions/${ids.session}/messages/`)) {
      if (method === 'GET') return pending[0] ? json(route, pending[0]) : json(route, { detail: 'message receipt not found' }, 404);
      if (path.endsWith('/resume')) { pending = []; return stream(route, [next() as unknown as Record<string, unknown>]); }
    }
    if (path === `/entries/${ids.entry}` && method === 'PATCH') {
      const changes = body?.sections as Record<string, { claims: string[]; open_question: string | null }>;
      for (const name of sections) if (changes[name]) entry[name] = { claims: changes[name].claims.map(text => ({ text, citations: [{ ...cite, turn_index: 4 }] })), open_question: changes[name].open_question };
      entry.status = 'draft'; entry.version++; entry.edited = true; entry.approved_at = null; entry.approved_by = null;
      entry.decisions!.push({ action: 'edit', who: expert, at, reason: null });
      return json(route, entry);
    }
    if (path === `/entries/${ids.entry}/approve`) {
      entry.status = 'approved'; entry.version++; entry.approved_at = at; entry.approved_by = expert;
      entry.decisions!.push({ action: 'approve', who: expert, at, reason: null });
      return json(route, entry);
    }
    if (path === `/entries/${ids.entry}/reject`) {
      entry.status = 'rejected'; entry.version++; entry.rejection_reason = String(body?.reason);
      entry.decisions!.push({ action: 'reject', who: expert, at, reason: entry.rejection_reason });
      return json(route, entry);
    }
    if (path === `/entries/${ids.entry}`) {
      if (reader) {
        if (entry.status !== 'approved') return json(route, { detail: 'resource not found' }, 404);
        const { decisions: _decisions, unsupported: _unsupported, session_id: _session, rejection_reason: _reason, ...publicEntry } = entry;
        return json(route, publicEntry);
      }
      return json(route, entry);
    }
    if (path === '/runbook/entries') return json(route, entry.status === 'approved' ? [{ id: entry.id, title: entry.title, version: entry.version, status: 'approved' }] : []);
    if (path === '/runbook/search') return json(route, { embedding_model: 'hashing', results: [{ id: entry.id, title: entry.title, version: entry.version, section: 'steps', snippet: entry.steps.claims[0].text, score: 1, citations: [cite] }] });
    if (path === '/runbook/ask') return json(route, { status: 'answered', answer: `1. ${entry.steps.claims[0].text} [E1]`, mode: 'extractive', evidence: [{ id: 'E1', entry_id: entry.id, title: entry.title, version: entry.version, section: 'steps', claim_index: 0, text: entry.steps.claims[0].text, citations: [cite] }], open_questions: [] });
    if (path === '/runbook/export' || path.endsWith('/markdown')) return route.fulfill({ status: 200, contentType: 'text/markdown', headers: { 'Content-Disposition': 'attachment; filename="eki-runbook.md"' }, body: `# ${entry.title}\n\n${entry.steps.claims[0].text}\n` });
    return json(route, { detail: 'Synthetic endpoint not implemented' }, 404);
  });
  return { requests, plan, session, entry };
}

export async function signIn(page: Page, path = `${workspace}/plans`, token = 'owner-token') {
  await page.goto(path);
  await page.getByLabel('Access token', { exact: true }).fill(token);
  await page.getByRole('button', { name: 'Open workspace' }).click();
  await expect(page.getByRole('heading', { name: 'Sign in', exact: true })).toHaveCount(0);
}
