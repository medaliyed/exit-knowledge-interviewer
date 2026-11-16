// F22 real API acceptance. Invoked by test_follow_up_browser.py; no network mocks.
const assert = require('node:assert/strict');
const { chromium, expect } = require('../frontend/node_modules/@playwright/test');
const options = JSON.parse(process.env.EKI_BROWSER_CASE);

async function signIn(page, subject) {
  await page.goto(options.origin);
  await page.getByLabel('Email', { exact: true }).fill(`${subject}@example.com`);
  await page.getByLabel('Password', { exact: true }).fill(options.password);
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  await expect(page.getByText(`Signed in as ${subject === 'owner' ? 'Nadia Ferrand' : 'Reader'}`)).toBeVisible();
}

(async () => {
  const browser = await chromium.launch(process.platform === 'darwin' ? { channel: 'chrome' } : {});
  const context = await browser.newContext();
  const page = await context.newPage();
  let currentPage = page;
  const failures = [];
  page.on('pageerror', error => failures.push(error.message));
  page.on('console', message => {
    if (message.type() === 'error' && /Content Security Policy|violates.*directive|Refused.*inline/i.test(message.text())) failures.push(message.text());
  });
  try {
    await signIn(page, 'reader');
    const question = 'How do I roll back the billing export?';
    await page.getByLabel('Your question', { exact: true }).fill(question);
    await page.getByRole('button', { name: 'Ask', exact: true }).click();
    await page.getByLabel('Send follow-up request to').selectOption(options.run);
    await page.getByRole('button', { name: 'Save for a follow-up', exact: true }).click();
    await expect(page.getByText('Request saved. It is awaiting owner triage.', { exact: true })).toBeVisible();
    await expect(page.getByRole('link', { name: 'Follow-ups', exact: true })).toHaveCount(0);

    const ownerContext = await browser.newContext();
    const owner = await ownerContext.newPage();
    currentPage = owner;
    owner.on('pageerror', error => failures.push(error.message));
    await signIn(owner, 'owner');
    await owner.getByRole('link', { name: 'Follow-ups', exact: true }).click();
    const request = owner.getByRole('article').filter({ has: owner.getByRole('heading', { name: question, exact: true }) });
    await request.getByRole('button', { name: 'Triage question', exact: true }).click();
    const triage = owner.getByRole('dialog', { name: 'Triage question', exact: true });
    await triage.getByLabel('Target entry', { exact: true }).selectOption(options.entry);
    await triage.getByLabel('Target section', { exact: true }).selectOption('rollback');
    await triage.getByRole('button', { name: 'Save triage', exact: true }).click();
    await expect(triage).toHaveCount(0);
    await owner.getByLabel(`Select gap: ${question}`, { exact: true }).check();
    await owner.getByRole('checkbox', { name: /Select gap: I never had to roll it back/ }).check();
    await owner.screenshot({ path: `${options.screenshots}/followup-queue.png`, fullPage: true });
    await owner.getByRole('button', { name: 'Prepare follow-up plan', exact: true }).click();
    await expect(owner.getByRole('heading', { name: 'Follow-up plan', exact: true })).toBeVisible();
    await owner.getByRole('button', { name: 'Approve plan', exact: true }).click();
    await owner.getByRole('dialog').getByRole('button', { name: 'Approve and start', exact: true }).click();
    for (let answer = 0; answer < 2; answer++) {
      await owner.getByRole('textbox', { name: 'Your answer', exact: true }).fill(options.answer);
      await owner.getByRole('button', { name: 'Send', exact: false }).click();
      if (answer === 0) await expect(owner.getByRole('textbox', { name: 'Your answer', exact: true })).toHaveValue('');
    }
    await owner.getByRole('link', { name: 'Review follow-up proposals', exact: false }).click();
    await expect(owner.getByRole('heading', { name: 'Follow-up proposals', exact: true })).toBeVisible();
    await expect(owner.getByText(options.answer, { exact: true }).first()).toBeVisible();
    await owner.setViewportSize({ width: 360, height: 780 });
    assert(await owner.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    await owner.screenshot({ path: `${options.screenshots}/followup-proposal-mobile.png`, fullPage: true });
    await owner.setViewportSize({ width: 1280, height: 900 });
    await owner.getByLabel('Apply rollback', { exact: true }).check();
    await owner.getByRole('button', { name: 'Apply selected sections', exact: true }).click();
    await owner.getByRole('link', { name: 'Review updated entry', exact: true }).click();
    await expect(owner.getByRole('button', { name: 'Approve', exact: true })).toBeVisible();
    assert.equal((await context.request.get(`${options.origin}/entries/${options.entry}`)).status(), 404);
    await owner.locator('#section-rollback').getByRole('button', { name: /interview · turn/ }).first().click();
    await expect(owner.getByRole('dialog').getByText(options.answer, { exact: true })).toBeVisible();
    await owner.keyboard.press('Escape');
    await expect(owner.getByRole('dialog')).toHaveCount(0);
    await owner.getByRole('button', { name: 'Approve', exact: true }).click();
    await expect(owner.getByRole('button', { name: 'Download as Markdown', exact: true })).toBeVisible();
    await owner.getByRole('link', { name: 'Follow-ups', exact: true }).click();
    await expect(owner.getByRole('checkbox', { name: /Select gap: I never had to roll it back/ })).toHaveCount(0);
    await owner.getByRole('button', { name: 'Resolve with approved entry', exact: true }).click();
    const resolution = owner.getByRole('dialog', { name: 'Resolve reader request', exact: true });
    await resolution.getByLabel('Approved entry that answers this question', { exact: true }).selectOption(options.entry);
    await resolution.getByRole('checkbox', { name: /I reviewed this approved version/ }).check();
    await resolution.getByRole('button', { name: 'Confirm request resolved', exact: true }).click();
    await expect(resolution).toHaveCount(0);
    await page.getByRole('button', { name: 'Refresh request status', exact: true }).click();
    await expect(page.getByText('Status: resolved.', { exact: false })).toBeVisible();
    const published = await context.request.get(`${options.origin}/entries/${options.entry}`);
    assert.equal(published.status(), 200);
    const entry = await published.json();
    assert(entry.rollback.claims.some(claim => claim.text === options.answer));
    assert(!('decisions' in entry) && !('unsupported' in entry));
    const citation = entry.rollback.claims[0].citations[0];
    assert.notEqual(citation.session_id, options.parent);
    assert.equal((await context.request.get(`${options.origin}/sessions/${citation.session_id}`)).status(), 404);
    const exported = await context.request.get(`${options.origin}/entries/${options.entry}/markdown`);
    assert.equal(exported.status(), 200);
    assert((await exported.text()).replaceAll('\\', '').includes(options.answer));
    assert.deepEqual(failures, []);
  } catch (error) {
    await currentPage.screenshot({ path: `${options.screenshots}/followup-failure.png`, fullPage: true });
    console.error(await currentPage.locator('body').innerText());
    throw error;
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
