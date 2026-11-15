import { expect, test, type Page } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';
import { ids, mockWorkspace, workspace } from './fixtures';
import type { ReaderFlag } from '../src/types';

async function maintenance(page: Page, role: 'owner' | 'reader' | 'admin' = 'owner', withFlag = false) {
  const mock = await mockWorkspace(page, { approvedEntry: true, approvedPlan: true });
  const entry = Object.assign(mock.entry, { maintenance_revision: 1, can_review: role === 'owner', review_by: '2026-11-14', last_reviewed_at: '2026-08-16T10:00:00Z', last_reviewed_by_name: 'Nadia Ferrand', owner_name: 'Samir', owner_subject: 'samir', freshness: 'overdue' as const, maintenance_started_at: '2026-11-14T10:00:00Z' });
  const state = { owner_subject: 'samir', owner_name: 'Samir', owner_unavailable: false, maintenance_started_at: entry.maintenance_started_at, run_revision: 2, history: [] as object[] };
  let flags: ReaderFlag[] = withFlag ? [{ id: '55555555-5555-4555-8555-555555555555', entry_id: entry.id, request_id: '66666666-6666-4666-8666-666666666666', observed_version: entry.version, category: 'unclear', section: 'steps', description: 'Does this command still apply?', reporter_subject: 'reader', created_at: '2026-11-15T10:00:00Z', resolution: null, resolved_at: null }] : [];
  let reviewed = false;
  const requests: { path: string; body: Record<string, unknown> }[] = [];
  const principal = { subject: role === 'reader' ? 'reader' : role === 'admin' ? 'operator' : 'samir', name: role === 'reader' ? 'Ari' : role === 'admin' ? 'Operator' : 'Samir', admin: role === 'admin' };
  await page.route('**/*', async route => {
    const path = new URL(route.request().url()).pathname;
    const method = route.request().method();
    const body = (route.request().postDataJSON() ?? {}) as Record<string, unknown>;
    const json = (value: unknown, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(value) });
    if (method === 'POST') requests.push({ path, body });
    if (path === '/auth/session') return json(principal);
    if (path === '/runs') return json([{ id: ids.run, expert_name: 'Nadia Ferrand', since: '2024-11-01', until: '2026-10-31', status: 'completed', can_manage: role !== 'reader', maintenance_started_at: state.maintenance_started_at, owner_name: state.owner_name }]);
    if (path === `/runs/${ids.run}/maintenance`) return json(state);
    if (path === `/runs/${ids.run}/maintenance/entries`) return json({ entries: [entry], next_offset: null });
    if (path === `/runs/${ids.run}/handover`) {
      expect(body.expected_owner_subject).toBe('samir'); expect(body.expected_run_revision).toBe(2);
      state.owner_name = String(body.owner_subject); state.owner_subject = state.owner_name; state.run_revision++;
      return json(state);
    }
    if (path === `/entries/${ids.entry}`) {
      if (role === 'reader') { const { decisions: _decisions, unsupported: _unsupported, ...safe } = entry; return json(safe); }
      return json(entry);
    }
    if (path.endsWith('/resolve') && method === 'POST') {
      if (body.resolution === 'confirmed_current' && !reviewed) return json({ detail: 'Review the current version before confirming this flag' }, 409);
      expect(body.expected_maintenance_revision).toBe(entry.maintenance_revision);
      flags[0].resolution = body.resolution as ReaderFlag['resolution']; flags[0].resolution_note = String(body.note); entry.maintenance_revision++;
      return json({ id: flags[0].id, resolution: flags[0].resolution });
    }
    if (path === `/entries/${ids.entry}/flags` && method === 'GET') return json({ flags });
    if (path === `/entries/${ids.entry}/flags` && method === 'POST') {
      expect(body.expected_version).toBe(entry.version);
      const flag: ReaderFlag = { id: '55555555-5555-4555-8555-555555555555', entry_id: entry.id, request_id: String(body.request_id), observed_version: entry.version, category: body.category as ReaderFlag['category'], section: null, description: String(body.description), created_at: '2026-11-15T10:00:00Z', resolution: null, resolved_at: null };
      flags = [flag]; entry.maintenance_revision++; return json(flag, 201);
    }
    if (path.startsWith('/flags/')) return json(flags[0]);
    if (path === `/entries/${ids.entry}/review`) {
      expect(body.expected_version).toBe(entry.version); expect(body.expected_maintenance_revision).toBe(entry.maintenance_revision);
      reviewed = true; entry.maintenance_revision++; entry.review_by = String(body.review_by); entry.last_reviewed_by_name = 'Samir'; entry.last_reviewed_at = '2026-11-15T11:00:00Z';
      Object.assign(entry, { freshness: 'current' }); return json(entry);
    }
    if (path === `/entries/${ids.entry}/withdraw`) {
      expect(body.expected_version).toBe(entry.version); expect(body.reason).toBe('Checking an obsolete command');
      entry.status = 'draft'; entry.approved_by = null; entry.approved_at = null; entry.version++; entry.maintenance_revision++; return json(entry);
    }
    return route.fallback();
  });
  return { entry, requests };
}

test('successor renews an overdue entry without changing original approval', async ({ page }) => {
  const mock = await maintenance(page);
  await page.goto(`${workspace}/entries/${ids.entry}`);
  await expect(page.getByText('Review overdue', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Reviewed; no changes needed' }).click();
  const dialog = page.getByRole('dialog', { name: 'Review without content changes?' });
  await dialog.getByRole('button', { name: 'Confirm review' }).click();
  await expect(dialog).toHaveCount(0);
  await expect(page.getByText('Review current', { exact: true })).toBeVisible();
  expect(mock.entry.approved_by).toBe('Nadia Ferrand'); expect(mock.entry.version).toBe(1);
  await expect(page.getByText('Only Nadia Ferrand can approve.', { exact: false })).toHaveCount(0);
});

test('reader submits feedback and sees a safe durable receipt', async ({ page }) => {
  const mock = await maintenance(page, 'reader');
  await page.goto(`${workspace}/entries/${ids.entry}`);
  await expect(page.getByRole('button', { name: 'Reviewed; no changes needed' })).toHaveCount(0);
  await page.getByRole('button', { name: 'Report a problem', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: 'Report a problem' });
  await dialog.getByLabel('Describe the problem').fill('The old restore command is unclear.');
  await dialog.getByRole('button', { name: 'Submit report' }).click();
  await expect(dialog).toHaveCount(0);
  await expect(page.getByText('Report saved', { exact: false })).toBeVisible();
  expect(mock.requests.find(item => item.path.endsWith('/flags'))?.body.request_id).toMatch(/^[a-f0-9-]{36}$/);
  await page.getByRole('button', { name: 'Check report status' }).click();
  await expect(page.getByRole('heading', { name: 'Reader feedback', exact: true })).toHaveCount(0);
});

test('withdrawal requires a reason and explicitly returns entry to draft', async ({ page }) => {
  const mock = await maintenance(page);
  await page.goto(`${workspace}/entries/${ids.entry}`);
  await page.getByRole('button', { name: 'Withdraw to draft' }).click();
  const dialog = page.getByRole('dialog', { name: 'Withdraw this entry?' });
  await dialog.getByLabel('Reason for withdrawal').fill('Checking an obsolete command');
  expect(mock.requests.filter(item => item.path.endsWith('/withdraw'))).toHaveLength(0);
  await dialog.getByRole('button', { name: 'Withdraw entry', exact: true }).click();
  await expect(dialog).toHaveCount(0); expect(mock.entry.status).toBe('draft');
  await expect(page.getByRole('button', { name: 'Approve', exact: true })).toBeVisible();
});

test('administrator reviews a concrete handover before committing it', async ({ page }) => {
  const mock = await maintenance(page, 'admin');
  await page.goto(`${workspace}/maintenance`);
  await page.getByRole('button', { name: 'Transfer responsibility' }).click();
  const dialog = page.getByRole('dialog', { name: 'Transfer responsibility?' });
  await expect(dialog.getByText('a new owner (enter a subject below)', { exact: false })).toBeVisible();
  await dialog.getByLabel('New owner subject').fill('lee'); await dialog.getByLabel('Reason for transfer').fill('Owner rotation');
  await expect(dialog.getByText(/^From .+ to lee\.$/)).toBeVisible();
  expect(mock.requests.filter(item => item.path.endsWith('/handover'))).toHaveLength(0);
  await dialog.getByRole('button', { name: 'Transfer and start maintenance' }).click();
  await expect(dialog).toHaveCount(0); await expect(page.getByText('Current owner: lee.', { exact: false })).toBeVisible();
});

test('owner reviews current content before resolving private reader feedback', async ({ page }) => {
  await maintenance(page, 'owner', true);
  await page.goto(`${workspace}/entries/${ids.entry}`);
  await expect(page.getByRole('heading', { name: 'Reader feedback', exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Reviewed; no changes needed' }).click();
  await page.getByRole('dialog').getByRole('button', { name: 'Confirm review' }).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await page.getByRole('button', { name: 'Resolve feedback' }).click();
  const dialog = page.getByRole('dialog', { name: 'Resolve reader feedback' });
  await dialog.getByLabel('Resolution', { exact: true }).selectOption('confirmed_current');
  await dialog.getByLabel('Private resolution reason').fill('Reviewed command against current procedure.');
  await dialog.getByRole('button', { name: 'Save resolution' }).click();
  await expect(dialog).toHaveCount(0);
  await expect(page.getByText('Confirmed current', { exact: true })).toBeVisible();
  await expect(page.getByText('Private resolution: Reviewed command against current procedure.')).toBeVisible();
});

for (const width of [360, 768, 1440]) {
  test(`maintenance queue is accessible at ${width}px`, async ({ page }) => {
    await maintenance(page); await page.setViewportSize({ width, height: 900 });
    await page.goto(`${workspace}/maintenance`);
    await expect(page.getByRole('heading', { name: 'Maintenance', exact: true })).toBeVisible();
    expect((await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa']).analyze()).violations).toEqual([]);
    if (width === 360) await page.screenshot({ path: '/tmp/phase8-maintenance-360.png', fullPage: true });
  });
}
