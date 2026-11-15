import { expect, test, type Page } from '@playwright/test';
import { ids, mockWorkspace, signIn, workspace } from './fixtures';

// Does the page object to being unloaded right now? Dispatching a cancelable
// beforeunload event mirrors what the browser does on refresh or tab close.
const unloadWarned = (page: Page) => page.evaluate(() => {
  const event = new Event('beforeunload', { cancelable: true });
  window.dispatchEvent(event);
  return event.defaultPrevented;
});

test('an unsent answer warns before the tab is refreshed or closed', async ({ page }) => {
  await mockWorkspace(page, { approvedPlan: true });
  await signIn(page, `${workspace}/interview/${ids.session}`);
  const answer = page.getByRole('textbox', { name: 'Your answer', exact: true });
  await expect(answer).toBeVisible();
  expect(await unloadWarned(page)).toBe(false);
  await answer.fill('A synthetic draft that has not been sent yet.');
  expect(await unloadWarned(page)).toBe(true);
  // The draft survives leaving the interview inside the workspace, so the warning stays.
  await page.getByRole('link', { name: 'Leave for now', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Interview plans', exact: true })).toBeVisible();
  expect(await unloadWarned(page)).toBe(true);
  await page.getByRole('link', { name: /Continue interview/ }).click();
  await page.getByRole('button', { name: 'Send', exact: false }).click();
  await expect(page.getByRole('heading', { name: 'How do we roll it back?' })).toBeVisible();
  expect(await unloadWarned(page)).toBe(false);
});

test('signing out with an unsent answer asks first and keeps the draft when declined', async ({ page }) => {
  await mockWorkspace(page, { approvedPlan: true });
  await signIn(page, `${workspace}/interview/${ids.session}`);
  const answer = page.getByRole('textbox', { name: 'Your answer', exact: true });
  await answer.fill('Unsent synthetic text.');
  await page.getByRole('button', { name: 'Sign out', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: 'Sign out with an unsent answer?' });
  await expect(dialog).toBeVisible();
  await dialog.getByRole('button', { name: 'Keep writing' }).click();
  await expect(dialog).toHaveCount(0);
  await expect(answer).toHaveValue('Unsent synthetic text.');
  await page.getByRole('button', { name: 'Sign out', exact: true }).click();
  await page.getByRole('dialog').getByRole('button', { name: 'Sign out and discard' }).click();
  await expect(page.getByRole('heading', { name: 'Sign in', exact: true })).toBeVisible();
});

test('a success notice clears itself and does not cover the composer buttons', async ({ page }) => {
  await mockWorkspace(page, { approvedPlan: true });
  await signIn(page, `${workspace}/interview/${ids.session}`);
  await page.getByRole('textbox', { name: 'Your answer', exact: true }).fill('A synthetic answer.');
  await page.getByRole('button', { name: 'Send', exact: false }).click();
  const notice = page.locator('.global-notice');
  await expect(notice).toContainText('Answer saved.');
  const skip = page.getByRole('button', { name: 'Skip this question', exact: true });
  await skip.scrollIntoViewIfNeeded();
  const [noticeBox, skipBox] = await Promise.all([notice.boundingBox(), skip.boundingBox()]);
  expect(noticeBox && skipBox && noticeBox.y + noticeBox.height <= skipBox.y, 'the notice must sit above the composer, not over it').toBe(true);
  await expect(notice).toBeEmpty({ timeout: 10_000 });
});

test('removing a topic asks for confirmation that names the topic', async ({ page }) => {
  const mock = await mockWorkspace(page);
  await signIn(page, `${workspace}/plans/${ids.plan}`);
  const card = page.locator('.topic-list > li', { hasText: 'Backup ownership' });
  await card.getByRole('button', { name: 'Remove topic', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: 'Remove this topic?' });
  await expect(dialog).toContainText('Backup ownership');
  await expect(dialog).toContainText('1 drafted question');
  expect(mock.requests.filter(request => request.method === 'PATCH')).toHaveLength(0);
  await dialog.getByRole('button', { name: 'Keep topic' }).click();
  await expect(dialog).toHaveCount(0);
  await expect(card).toBeVisible();
  await card.getByRole('button', { name: 'Remove topic', exact: true }).click();
  await page.getByRole('dialog').getByRole('button', { name: 'Remove topic', exact: true }).click();
  await expect(page.locator('.topic-list > li', { hasText: 'Backup ownership' })).toHaveCount(0);
  expect(mock.requests.find(request => request.method === 'PATCH')?.body).toEqual({ remove: ['topic:backup'] });
});

test('an entry page highlights the tab it belongs to', async ({ page }) => {
  await mockWorkspace(page, { approvedPlan: true, approvedEntry: true });
  await signIn(page, `${workspace}/entries/${ids.entry}`);
  await expect(page.locator('.tabs a.active')).toHaveText('Review entries');
  await page.getByRole('button', { name: 'Sign out', exact: true }).click();
  await signIn(page, `${workspace}/entries/${ids.entry}`, 'reader-token');
  await expect(page.locator('.tabs a.active')).toHaveText('Approved knowledge');
});

test('the review page names conversations without internal identifiers', async ({ page }) => {
  await mockWorkspace(page, { approvedPlan: true });
  await signIn(page, `${workspace}/review`);
  await expect(page.getByRole('heading', { name: 'Review entries', exact: true })).toBeVisible();
  const visible = await page.evaluate(() => document.body.innerText);
  expect(visible).not.toContain(ids.session.slice(0, 8));
});

test('the transcript column explains itself before the first answer', async ({ page }) => {
  await mockWorkspace(page, { approvedPlan: true });
  await signIn(page, `${workspace}/interview/${ids.session}`);
  const transcript = page.getByRole('complementary', { name: 'Conversation so far' });
  await expect(transcript).toContainText('Your answers will appear here');
  await page.getByRole('textbox', { name: 'Your answer', exact: true }).fill('A synthetic answer.');
  await page.getByRole('button', { name: 'Send', exact: false }).click();
  await expect(page.getByRole('heading', { name: 'How do we roll it back?' })).toBeVisible();
  await expect(transcript).not.toContainText('Your answers will appear here');
});

test('tabs stay fully readable at phone width', async ({ page }) => {
  await page.setViewportSize({ width: 360, height: 800 });
  await mockWorkspace(page, { approvedPlan: true, approvedEntry: true });
  await signIn(page);
  const knowledge = page.getByRole('link', { name: 'Approved knowledge', exact: true });
  const [tabs, box] = await Promise.all([page.locator('.tabs').boundingBox(), knowledge.boundingBox()]);
  expect(tabs && box && box.x + box.width <= tabs.x + tabs.width + 0.5, 'the last tab must not be clipped').toBe(true);
  expect(await page.locator('.tabs').evaluate(element => element.scrollWidth <= element.clientWidth)).toBe(true);
});
