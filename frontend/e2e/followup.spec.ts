import { expect, test, type Page, type Route } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';
import { expert, ids, mockWorkspace, signIn, workspace } from './fixtures';

const fid = '55555555-5555-4555-8555-555555555555';
const gid = '66666666-6666-4666-8666-666666666666';
const pid = '77777777-7777-4777-8777-777777777777';
const planId = '88888888-8888-4888-8888-888888888888';
const question = 'How do I restore the export?';

async function followUpMock(page: Page, stale = false, active = false) {
  const mock = await mockWorkspace(page, { approvedPlan: true, approvedEntry: true });
  mock.session.status = 'finished'; mock.session.waiting = null;
  const calls: { path: string; body: Record<string, unknown> }[] = [];
  const gap = { id: gid, kind: 'request', question, seed_question: question, status: 'pending', revision: 1, target_entry_id: ids.entry, target_section: 'rollback', priority: 0, attempts: [] };
  let running = active;
  const claim = { text: 'Restore the pre-run export.', citations: [{ kind: 'interview_turn', session_id: fid, turn_index: 2 }] };
  const plan = { ...mock.plan, id: planId, kind: 'follow_up', revision: 1, parent_session_id: ids.session, status: 'draft', limits: { topics: 3, questions: 6, model_calls: 12, embedding_calls: 6 }, topics: [{ ...mock.plan.topics[0], origin: 'follow_up', sections: ['rollback'], questions: [{ text: question, origin: 'follow_up', gap_id: gid, section: 'rollback', artifact_id: null, quote: null }] }] };
  const proposal = { id: pid, session_id: fid, entry_id: ids.entry, title: mock.entry.title, status: 'pending', revision: 1, base_version: 1, entry_version: stale ? 2 : 1, stale, can_apply: true, entry_status: 'approved', sections: [{ section: 'rollback', base: mock.entry.rollback, added: [claim], merged: { claims: [...mock.entry.rollback.claims, claim], open_question: null }, replaced: { claims: [claim], open_question: null }, clears_open: true }], unchanged_sections: ['trigger', 'diagnosis', 'steps', 'contacts', 'pitfalls', 'rationale'], unsupported: [], applied_version: null };
  const json = (route: Route, value: unknown, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(value) });
  await page.route('**/*', async route => {
    const path = new URL(route.request().url()).pathname;
    const method = route.request().method();
    const body = route.request().postDataJSON() as Record<string, unknown> | null;
    if (method !== 'GET' && /follow-up|\/stop$/.test(path)) calls.push({ path, body: body ?? {} });
    if (path === `/runs/${ids.run}/follow-up-requests`) return json(route, { ...gap, run_id: ids.run }, 201);
    if (path === `/follow-up-requests/${gid}`) return json(route, gap);
    if (path === `/runs/${ids.run}/follow-up-gaps`) return json(route, { gaps: [gap], next_offset: null });
    if (path === `/runs/${ids.run}/follow-up-gaps/${gid}` && method === 'PATCH') { gap.revision++; if (body?.action === 'defer') gap.status = 'deferred'; if (body?.action === 'reopen') gap.status = 'pending'; return json(route, gap); }
    if (path === `/runs/${ids.run}/follow-up-gaps/${gid}/resolve`) { gap.status = 'resolved'; return json(route, gap); }
    if (path === `/runs/${ids.run}/follow-ups`) return json(route, plan, 201);
    if (path === `/plans/${planId}`) return json(route, plan);
    if (path === `/plans/${planId}/approve`) return json(route, { id: fid });
    if (path === `/sessions/${fid}/stop`) { running = false; return json(route, { completion_reason: 'expert_stopped' }); }
    if (path === `/sessions/${fid}`) return json(route, { ...mock.session, status: running ? 'active' : 'finished', waiting: running ? { turn: 1, question, section: 'rollback', topic_id: mock.plan.topics[0].topic_id } : null, id: fid, plan_id: planId, kind: 'follow_up', scope: ['rollback'], usage: { questions: 1, model_calls: 2, embedding_calls: 0, limits: plan.limits }, completion_reason: active ? 'expert_stopped' : 'question_limit' });
    if (path === `/sessions/${fid}/messages`) return json(route, []);
    if (path === `/sessions/${fid}/entries`) return json(route, []);
    if (path === `/sessions/${fid}/follow-up-proposals`) return json(route, { session_id: fid, proposals: [proposal] });
    if (path === `/follow-up-proposals/${pid}/refresh`) { proposal.stale = false; proposal.revision++; proposal.base_version = proposal.entry_version; return json(route, proposal); }
    if (path === `/follow-up-proposals/${pid}/apply`) { proposal.status = 'applied'; mock.entry.status = 'draft'; mock.entry.version++; mock.entry.rollback.claims.push(claim as typeof mock.entry.rollback.claims[number]); return json(route, { entry: mock.entry, proposal }); }
    if (path === '/runbook/ask') return json(route, { status: 'insufficient_evidence', reason: 'no_supported_answer', answer: '', mode: 'extractive', evidence: [], open_questions: [] });
    return route.fallback();
  });
  return { ...mock, calls, gap, plan, proposal };
}

test('reader saves an unanswered question explicitly to the selected handover', async ({ page }) => {
  const mock = await followUpMock(page);
  await signIn(page, `${workspace}/knowledge`, 'reader-token');
  await page.getByLabel('Your question', { exact: true }).fill(question);
  await page.getByRole('button', { name: 'Ask', exact: true }).click();
  await page.getByLabel('Send follow-up request to').selectOption(ids.run);
  await expect(page.getByText(`Owner: ${expert}`, { exact: false })).toBeVisible();
  await page.getByRole('button', { name: 'Save for a follow-up', exact: true }).click();
  await expect(page.getByText('Request saved. It is awaiting owner triage.', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Refresh request status', exact: true }).click();
  expect(mock.calls[0].body).toMatchObject({ question });
  expect(mock.calls[0].path).toBe(`/runs/${ids.run}/follow-up-requests`);
  await expect(page.getByRole('link', { name: 'Follow-ups', exact: true })).toHaveCount(0);
});

test('owner selects a gap and reviews a focused plan with frozen limits', async ({ page }) => {
  const mock = await followUpMock(page);
  await signIn(page, `${workspace}/follow-ups`);
  await page.getByLabel(`Select gap: ${question}`).check();
  await page.getByRole('button', { name: 'Prepare follow-up plan', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Follow-up plan', exact: true })).toBeVisible();
  await expect(page.getByText('6 questions', { exact: false }).first()).toBeVisible();
  await expect(page.getByText('rollback', { exact: true }).first()).toBeVisible();
  expect(mock.calls.find(call => call.path.endsWith('/follow-ups'))?.body).toMatchObject({ parent_session_id: ids.session, gap_ids: [gid] });
});

test('stale proposal requires explicit refresh, then selected apply creates a reviewable draft', async ({ page }) => {
  const mock = await followUpMock(page, true);
  await signIn(page, `${workspace}/follow-ups/${fid}`);
  await expect(page.getByText('Restore the pre-run export.', { exact: true }).first()).toBeVisible();
  await expect(page.getByRole('button', { name: 'Apply selected sections', exact: true })).toBeDisabled();
  await page.getByRole('button', { name: 'Refresh proposal to current version', exact: true }).click();
  await page.getByLabel('Apply rollback').check();
  await page.getByRole('button', { name: 'Apply selected sections', exact: true }).click();
  await expect(page.getByRole('link', { name: 'Review updated entry', exact: true })).toBeVisible();
  expect(mock.calls.find(call => call.path.endsWith('/apply'))?.body).toMatchObject({ expected_revision: 2, expected_version: 2, applied_by: expert, sections: { rollback: 'merge' } });
});

test('owner reads the cited follow-up answer before applying a proposal', async ({ page }) => {
  const mock = await followUpMock(page);
  mock.session.turns.push({ ordinal: 2, topic_id: mock.plan.topics[0].topic_id, speaker: 'expert', kind: 'answer', text: 'Restore the pre-run export.', confidential: false, section: null, answers: 1, corrects: null });
  await signIn(page, `${workspace}/follow-ups/${fid}`);
  await page.locator('.proposal-comparison > div').nth(1).getByRole('button', { name: 'interview · turn 2', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: 'Source answer · turn 2', exact: true });
  await expect(dialog).toContainText('Restore the pre-run export.');
  await expect(dialog).toContainText(fid);
  expect(mock.calls.filter(call => call.path.endsWith('/apply'))).toEqual([]);
});

test('follow-up completion shows partial outcome and proposal review instead of ordinary entries', async ({ page }) => {
  await followUpMock(page);
  await signIn(page, `${workspace}/interview/${fid}`);
  await expect(page.getByText('Question limit reached', { exact: true })).toBeVisible();
  await expect(page.getByRole('link', { name: 'Review follow-up proposals', exact: false })).toHaveAttribute('href', `${workspace}/follow-ups/${fid}`);
  await expect(page.locator('.checklist li')).toHaveCount(1);
});

test('follow-up queue and comparison remain accessible at narrow widths', async ({ page }) => {
  await followUpMock(page);
  await page.setViewportSize({ width: 360, height: 780 });
  await signIn(page, `${workspace}/follow-ups`);
  await expect(page.getByRole('heading', { name: 'Follow-ups', exact: true })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  expect((await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa']).analyze()).violations).toEqual([]);
  await page.goto(`${workspace}/follow-ups/${fid}`);
  await expect(page.getByRole('heading', { name: 'Follow-up proposals', exact: true })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  expect((await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa']).analyze()).violations).toEqual([]);
});

for (const path of ['follow-ups', `follow-ups/${fid}`]) {
  test(`reader cannot open private ${path}`, async ({ page }) => {
    const mock = await followUpMock(page);
    await signIn(page, `${workspace}/${path}`, 'reader-token');
    await expect(page).toHaveURL(`${workspace}/knowledge`);
    expect(mock.calls).toEqual([]);
    await expect(page.getByRole('heading', { name: 'Follow-ups', exact: true })).toHaveCount(0);
  });
}

test('triage requires a deferral reason and reopens the saved revision', async ({ page }) => {
  const mock = await followUpMock(page);
  await signIn(page, `${workspace}/follow-ups`);
  const button = page.getByRole('button', { name: 'Triage question', exact: true });
  await button.focus(); await page.keyboard.press('Enter');
  const dialog = page.getByRole('dialog', { name: 'Triage question' });
  await dialog.getByLabel('Action', { exact: true }).selectOption('defer');
  await dialog.getByLabel('Reason', { exact: true }).fill('Ask during the next handover review.');
  await dialog.getByRole('button', { name: 'Save triage', exact: true }).click();
  await expect(dialog).not.toBeVisible();
  await page.getByRole('button', { name: 'Reopen gap', exact: true }).click();
  expect(mock.calls.filter(call => call.path.endsWith(gid)).map(call => call.body)).toEqual([
    { action: 'defer', expected_revision: 1, reason: 'Ask during the next handover review.' },
    { action: 'reopen', expected_revision: 2 },
  ]);
});

test('request resolution requires explicit confirmation of the approved version', async ({ page }) => {
  const mock = await followUpMock(page);
  mock.gap.status = 'awaiting_review';
  await signIn(page, `${workspace}/follow-ups`);
  await page.getByRole('button', { name: 'Resolve with approved entry', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: 'Resolve reader request' });
  const resolve = dialog.getByRole('button', { name: 'Confirm request resolved', exact: true });
  await expect(resolve).toBeDisabled();
  await dialog.getByRole('checkbox').check();
  await resolve.click();
  expect(mock.calls.find(call => call.path.endsWith('/resolve'))?.body).toEqual({ expected_revision: 1, entry_id: ids.entry, expected_version: 1, confirms_answer: true });
});

test('triage conflict closes the stale editor and reloads its revision before retry', async ({ page }) => {
  const mock = await followUpMock(page);
  let conflict = true;
  await page.route(`**/runs/${ids.run}/follow-up-gaps/${gid}`, async route => {
    if (route.request().method() === 'PATCH' && conflict) {
      conflict = false; mock.gap.revision++;
      return route.fulfill({ status: 409, contentType: 'application/json', body: JSON.stringify({ detail: 'gap changed' }) });
    }
    return route.fallback();
  });
  await signIn(page, `${workspace}/follow-ups`);
  await page.getByRole('button', { name: 'Triage question', exact: true }).click();
  let dialog = page.getByRole('dialog', { name: 'Triage question', exact: true });
  await dialog.getByRole('button', { name: 'Save triage', exact: true }).click();
  await expect(dialog).not.toBeVisible();
  await expect(page.getByRole('alert')).toContainText('saved version changed');
  await page.getByRole('button', { name: 'Triage question', exact: true }).click();
  dialog = page.getByRole('dialog', { name: 'Triage question', exact: true });
  await dialog.getByRole('button', { name: 'Save triage', exact: true }).click();
  await expect(dialog).not.toBeVisible();
  expect(mock.calls.find(call => call.body.action === 'map')?.body.expected_revision).toBe(2);
});

test('resolution conflict reloads the approved version and requires a new confirmation', async ({ page }) => {
  const mock = await followUpMock(page);
  let conflict = true;
  await page.route(`**/runs/${ids.run}/follow-up-gaps/${gid}/resolve`, async route => {
    if (conflict) {
      conflict = false; mock.entry.version++;
      return route.fulfill({ status: 409, contentType: 'application/json', body: JSON.stringify({ detail: 'entry changed' }) });
    }
    return route.fallback();
  });
  await signIn(page, `${workspace}/follow-ups`);
  await page.getByRole('button', { name: 'Resolve with approved entry', exact: true }).click();
  let dialog = page.getByRole('dialog', { name: 'Resolve reader request', exact: true });
  await dialog.getByRole('checkbox').check();
  await dialog.getByRole('button', { name: 'Confirm request resolved', exact: true }).click();
  await expect(dialog).not.toBeVisible();
  await page.getByRole('button', { name: 'Resolve with approved entry', exact: true }).click();
  dialog = page.getByRole('dialog', { name: 'Resolve reader request', exact: true });
  await expect(dialog.getByRole('button', { name: 'Confirm request resolved', exact: true })).toBeDisabled();
  await expect(dialog.getByRole('link', { name: 'Read approved version 2 (opens a new tab)', exact: true })).toBeVisible();
});

test('stopping a focused interview confirms unsent-answer loss and keeps captured work', async ({ page }) => {
  const mock = await followUpMock(page, false, true);
  await signIn(page, `${workspace}/interview/${fid}`);
  await page.getByLabel('Your answer', { exact: true }).fill('Unsent scratch answer.');
  await page.getByRole('button', { name: 'Stop for now', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: 'Stop this follow-up?' });
  await expect(dialog).toContainText('Your unsent answer will be discarded.');
  await dialog.getByRole('button', { name: 'Stop and keep captured answers', exact: true }).click();
  await expect(page.getByText('Stopped by expert', { exact: true })).toBeVisible();
  expect(mock.calls.some(call => call.path === `/sessions/${fid}/stop`)).toBe(true);
});

test('accepted answer stays locked until its delayed reply finishes', async ({ page }) => {
  await followUpMock(page, false, true);
  await page.addInitScript(({ sessionId }) => {
    const original = window.fetch;
    window.fetch = async (input, options) => {
      if (String(input).endsWith(`/sessions/${sessionId}/messages`) && options?.method === 'POST') {
        const body = JSON.parse(String(options.body));
        const encoder = new TextEncoder();
        const stream = new ReadableStream({ start(controller) {
          controller.enqueue(encoder.encode(`event: delivery\ndata: ${JSON.stringify({ type: 'delivery', id: body.message_id, status: 'saved' })}\n\n`));
          (window as Window & { releaseInterviewReply?: () => void }).releaseInterviewReply = () => {
            controller.enqueue(encoder.encode('event: done\ndata: {"type":"done"}\n\n')); controller.close();
          };
        } });
        return new Response(stream, { headers: { 'Content-Type': 'text/event-stream', 'X-EKI-Message-ID': body.message_id } });
      }
      return original(input, options);
    };
  }, { sessionId: fid });
  await signIn(page, `${workspace}/interview/${fid}`);
  const composer = page.getByLabel('Your answer', { exact: true });
  await composer.fill('Restore the pre-run export.');
  await page.getByRole('button', { name: 'Send', exact: false }).click();
  await expect(composer).toHaveValue('');
  await expect(composer).toBeDisabled();
  await expect(page.getByRole('button', { name: 'Stop for now', exact: true })).toBeDisabled();
  await page.evaluate(() => (window as Window & { releaseInterviewReply?: () => void }).releaseInterviewReply?.());
  await expect(composer).toBeEnabled();
});

for (const reason of ['evidence_changed', 'context_limit', 'review_required']) {
  test(`Ask distinguishes ${reason} without creating a knowledge-gap request`, async ({ page }) => {
    const mock = await followUpMock(page);
    await page.route('**/runbook/ask', route => route.fulfill({ contentType: 'application/json', body: JSON.stringify({ status: 'insufficient_evidence', reason, answer: '', evidence: [], open_questions: [], mode: 'extractive' }) }));
    await signIn(page, `${workspace}/knowledge`);
    await page.getByLabel('Your question', { exact: true }).fill(question);
    await page.getByRole('button', { name: 'Ask', exact: true }).click();
    await expect(page.locator('.gap-state strong')).toContainText(reason === 'evidence_changed' ? 'changed while answering' : reason === 'context_limit' ? 'exceeds the answer context' : 'needs review');
    await expect(page.getByRole('button', { name: 'Save for a follow-up', exact: true })).toHaveCount(0);
    expect(mock.calls).toEqual([]);
  });
}
