import { expect, test, type Page } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';
import {
  expert,
  ids,
  mockWorkspace,
  openAccountMenu,
  password,
  signIn,
  workspace,
} from './fixtures';

const policyConsoleErrors = new WeakMap<Page, string[]>();
type PolicyWindow = Window & { __ekiPolicyViolations?: string[] };

test.beforeEach(async ({ page }) => {
  const errors: string[] = [];
  policyConsoleErrors.set(page, errors);
  page.on('console', message => {
    if (
      message.type() === 'error' &&
      /Content Security Policy|violates.*directive|Refused.*inline/i.test(message.text())
    )
      errors.push(message.text());
  });
  await page.addInitScript(() => {
    const target = window as PolicyWindow;
    target.__ekiPolicyViolations = [];
    document.addEventListener('securitypolicyviolation', event => {
      target.__ekiPolicyViolations!.push(`${event.effectiveDirective}: ${event.blockedURI}`);
    });
  });
});

test.afterEach(async ({ page }) => {
  expect(policyConsoleErrors.get(page) ?? [], 'The UI must not emit CSP console errors').toEqual(
    [],
  );
  expect(
    await page.evaluate(() => (window as PolicyWindow).__ekiPolicyViolations ?? []),
    'The production policy must allow all UI behavior',
  ).toEqual([]);
});

test('sign-in rejects incorrect credentials and accepts an authorized identity without browser storage', async ({
  page,
}) => {
  await mockWorkspace(page);
  await page.goto('/');
  await page.getByLabel('Email', { exact: true }).fill('nadia.ferrand@example.com');
  await page.getByLabel('Password', { exact: true }).fill('incorrect-password');
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  await expect(page.getByRole('alert')).toContainText('Email or password is incorrect.');
  await page.getByLabel('Email', { exact: true }).fill('nadia.ferrand@example.com');
  await page.getByLabel('Password', { exact: true }).fill(password);
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Interview plans', exact: true })).toBeVisible();
  await expect(page.getByText(`Signed in as ${expert}`)).toBeVisible();
  expect(
    await page.evaluate(() => ({ local: localStorage.length, session: sessionStorage.length })),
  ).toEqual({ local: 0, session: 0 });
  await openAccountMenu(page);
  await page.getByRole('button', { name: 'Sign out', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Sign in', exact: true })).toBeVisible();
  await page.getByLabel('Email', { exact: true }).fill('reader@example.com');
  await page.getByLabel('Password', { exact: true }).fill(password);
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  await expect(page).toHaveURL(`${workspace}/knowledge`);
  await expect(page.getByRole('link', { name: 'Interview plans', exact: true })).toHaveCount(0);
  await expect(page.getByRole('link', { name: 'Review entries', exact: true })).toHaveCount(0);
});

for (const forbidden of ['plans', `plans/${ids.plan}`, `interview/${ids.session}`, 'review']) {
  test(`reader is redirected away from private ${forbidden} route`, async ({ page }) => {
    const mock = await mockWorkspace(page, { approvedEntry: true });
    await signIn(page, `${workspace}/${forbidden}`, 'reader-token');
    await expect(page).toHaveURL(`${workspace}/knowledge`);
    await expect(page.getByRole('link', { name: 'Review entries', exact: true })).toHaveCount(0);
    expect(mock.requests.filter(request => /^\/(plans|sessions)\//.test(request.path))).toEqual([]);
  });
}

test('plan review supports keyboard-operable reordering and explicit approval confirmation', async ({
  page,
}) => {
  const mock = await mockWorkspace(page);
  await signIn(page, `${workspace}/plans/${ids.plan}`);
  await expect(
    page.getByRole('heading', { name: 'Month-end reconciliation', exact: true }),
  ).toBeVisible();
  const move = page.getByRole('button', { name: 'Move Backup ownership up', exact: true });
  await move.focus();
  await page.keyboard.press('Enter');
  await expect.poll(() => mock.plan.topics[0].topic_id).toBe('topic:backup');
  await expect(page.locator('.topic-list > li').first()).toContainText('Backup ownership');
  await expect(page.locator('.topic-list > li').first()).toBeFocused();
  expect(mock.requests.find(request => request.method === 'PATCH')?.body).toEqual({
    order: ['topic:backup', 'topic:reconciliation'],
  });
  await page.getByRole('button', { name: 'Approve plan', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: 'Approve this plan?' });
  await expect(dialog).toBeVisible();
  expect(mock.requests.filter(request => request.path.endsWith('/approve'))).toHaveLength(0);
  await dialog.getByRole('button', { name: 'Keep editing' }).click();
  await expect(dialog).toHaveCount(0);
  await page.getByRole('button', { name: 'Approve plan', exact: true }).click();
  await page.getByRole('dialog').getByRole('button', { name: 'Approve and start' }).click();
  await expect(page).toHaveURL(`${workspace}/interview/${ids.session}`);
  await expect(page.getByRole('heading', { name: 'Who owns the backup decision?' })).toBeVisible();
  expect(
    mock.requests.find(request => request.path === `/plans/${ids.plan}/approve`)?.body,
  ).toEqual({ approved_by: expert });
});

test('accepted confidential answers are cleared and only the privacy placeholder is displayed', async ({
  page,
}) => {
  const mock = await mockWorkspace(page, { approvedPlan: true });
  await signIn(page, `${workspace}/interview/${ids.session}`);
  const secret = 'Synthetic private escalation detail 8392';
  await page.getByRole('textbox', { name: 'Your answer', exact: true }).fill(secret);
  await page.getByRole('switch', { name: /Confidential/ }).click();
  await page.getByRole('button', { name: 'Send confidentially' }).click();
  await expect(page.getByRole('heading', { name: 'How do we roll it back?' })).toBeVisible();
  await expect(page.getByRole('textbox', { name: 'Your answer', exact: true })).toHaveValue('');
  await expect(page.getByText('[confidential]', { exact: true })).toBeVisible();
  expect(await page.content()).not.toContain(secret);
  const submission = mock.requests.find(
    request => request.method === 'POST' && request.path.endsWith('/messages'),
  );
  expect(submission?.body).toMatchObject({
    action: 'answer',
    question_turn: 1,
    text: secret,
    confidential: true,
  });
  expect(submission?.body?.message_id).toMatch(/^[\da-f-]{36}$/);
  expect(mock.requests.every(request => !request.path.includes(secret))).toBe(true);
});

test('an accepted answer interrupted before the next question resumes without duplicate submission', async ({
  page,
}) => {
  const mock = await mockWorkspace(page, { approvedPlan: true, interruptMessage: true });
  await signIn(page, `${workspace}/interview/${ids.session}`);
  await page
    .getByRole('textbox', { name: 'Your answer', exact: true })
    .fill('A synthetic answer that must be saved once.');
  await page.getByRole('button', { name: 'Send', exact: false }).click();
  const resume = page.getByRole('button', { name: 'Continue', exact: true });
  await expect(resume).toBeVisible();
  await expect(page.getByText('Your answer is saved.', { exact: true }).first()).toBeVisible();
  expect(
    mock.requests.filter(
      request => request.method === 'POST' && request.path.endsWith('/messages'),
    ),
  ).toHaveLength(1);
  await resume.click();
  await expect(page.getByRole('heading', { name: 'How do we roll it back?' })).toBeVisible();
  expect(
    mock.requests.filter(
      request => request.method === 'POST' && request.path.endsWith('/messages'),
    ),
  ).toHaveLength(1);
  const resumeRequests = mock.requests.filter(
    request => request.method === 'POST' && request.path.endsWith('/resume'),
  );
  expect(resumeRequests).toHaveLength(1);
  expect(resumeRequests[0].body).toBeNull();
});

test('editing an approved section returns to draft and requires a separate approval', async ({
  page,
}) => {
  const mock = await mockWorkspace(page, { approvedPlan: true, approvedEntry: true });
  await signIn(page, `${workspace}/entries/${ids.entry}`);
  await page.getByRole('button', { name: 'Edit steps', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: 'Edit steps', exact: true });
  await expect(dialog).toContainText('Saving removes this approved entry from reader access');
  await dialog
    .getByLabel('Step 1', { exact: true })
    .fill('Stop the billing worker after checking the queue.');
  await dialog.getByRole('button', { name: 'Save section', exact: true }).click();
  await expect(dialog).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Approve', exact: true })).toBeVisible();
  expect(mock.entry.status).toBe('draft');
  expect(mock.requests.filter(request => request.path.endsWith('/approve'))).toHaveLength(0);
  expect(mock.requests.find(request => request.method === 'PATCH')?.body).toEqual({
    expected_version: 1,
    edited_by: expert,
    sections: {
      steps: { claims: ['Stop the billing worker after checking the queue.'], open_question: null },
    },
  });
  await page.getByRole('button', { name: 'Approve', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Download as Markdown' })).toBeVisible();
  expect(mock.entry.status).toBe('approved');
});

test('rejecting an entry requires a reason and records the explicit decision', async ({ page }) => {
  const mock = await mockWorkspace(page, { approvedPlan: true });
  await signIn(page, `${workspace}/entries/${ids.entry}`);
  await page.getByRole('button', { name: 'Reject', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: 'Reject this entry?' });
  await dialog.getByRole('button', { name: 'Reject entry', exact: true }).click();
  await expect(
    dialog.getByText('Add a short reason so the decision makes sense later.'),
  ).toBeVisible();
  expect(mock.requests.filter(request => request.path.endsWith('/reject'))).toHaveLength(0);
  await dialog.getByLabel('Reason', { exact: true }).fill('This synthetic procedure is outdated.');
  await dialog.getByRole('button', { name: 'Reject entry', exact: true }).click();
  await expect(dialog).toHaveCount(0);
  await expect(
    page.getByText('Rejected: This synthetic procedure is outdated.', { exact: false }),
  ).toBeVisible();
  expect(mock.requests.find(request => request.path.endsWith('/reject'))?.body).toEqual({
    expected_version: 1,
    rejected_by: expert,
    reason: 'This synthetic procedure is outdated.',
  });
});

test('approved knowledge searches, answers with cited evidence and downloads Markdown', async ({
  page,
}) => {
  const mock = await mockWorkspace(page, { approvedEntry: true });
  await signIn(page, `${workspace}/knowledge`, 'reader-token');
  await page.getByLabel('Key words', { exact: true }).fill('billing worker');
  await page.getByRole('button', { name: 'Search', exact: true }).click();
  await expect
    .poll(() => mock.requests.some(request => request.path === '/runbook/search'))
    .toBe(true);
  await expect(page.getByText('Stop the billing worker.', { exact: true }).first()).toBeVisible();
  await page.getByLabel('Your question', { exact: true }).fill('How do I stop the billing worker?');
  await page.getByRole('button', { name: /Ask/ }).click();
  await expect
    .poll(() => mock.requests.some(request => request.path === '/runbook/ask'))
    .toBe(true);
  await expect(page.getByText(/E1/).first()).toBeVisible();
  expect(mock.requests.find(request => request.path === '/runbook/ask')?.body).toEqual({
    question: 'How do I stop the billing worker?',
  });
  const downloadPromise = page.waitForEvent('download');
  await page
    .getByRole('button', { name: /Download.*Markdown/ })
    .first()
    .click();
  const download = await downloadPromise;
  expect(download.suggestedFilename()).toMatch(/\.md$/);
  expect(
    mock.requests.some(
      request => request.path === '/runbook/export' || request.path.endsWith('/markdown'),
    ),
  ).toBe(true);
  expect(mock.requests.filter(request => /^\/sessions\//.test(request.path))).toHaveLength(0);
  await page
    .locator('.evidence-list')
    .getByRole('link', { name: 'Month-end reconciliation', exact: true })
    .click();
  await expect(page).toHaveURL(`${workspace}/entries/${ids.entry}#section-steps`);
  await expect(page.locator('#section-steps')).toBeFocused();
});

for (const width of [360, 768, 1440]) {
  test(`core screens fit ${width}px and expose no WCAG A/AA violations`, async ({ page }) => {
    await page.setViewportSize({ width, height: 1000 });
    await mockWorkspace(page, { approvedPlan: true, approvedEntry: true });
    const check = async () => {
      await expect
        .poll(() => page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth))
        .toBe(true);
      const accessibility = await new AxeBuilder({ page })
        .withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa'])
        .analyze();
      expect(accessibility.violations).toEqual([]);
    };
    await page.goto('/');
    await expect(page.getByRole('heading', { name: 'Sign in', exact: true })).toBeVisible();
    await check();
    await page.getByLabel('Email', { exact: true }).fill('nadia.ferrand@example.com');
    await page.getByLabel('Password', { exact: true }).fill(password);
    await page.getByRole('button', { name: 'Sign in', exact: true }).click();
    await expect(page.getByRole('heading', { name: 'Interview plans', exact: true })).toBeVisible();
    await check();
    await page.getByRole('link', { name: /Continue interview/ }).click();
    await expect(
      page.getByRole('heading', { name: 'What starts the month-end reconciliation?' }),
    ).toBeVisible();
    await check();
    if (process.env.EKI_UI_QA_DIR && width === 1440)
      await page.screenshot({
        path: `${process.env.EKI_UI_QA_DIR}/interview-1440.png`,
        fullPage: true,
      });
    await page.getByRole('link', { name: 'Leave for now', exact: true }).click();
    await page.getByRole('link', { name: 'Review entries', exact: true }).click();
    await page.getByRole('link', { name: 'Month-end reconciliation', exact: true }).click();
    await expect(
      page.getByRole('heading', { name: 'Month-end reconciliation', exact: true }),
    ).toBeVisible();
    await check();
    if (process.env.EKI_UI_QA_DIR && width === 360)
      await page.screenshot({ path: `${process.env.EKI_UI_QA_DIR}/entry-360.png`, fullPage: true });
    await page.getByRole('button', { name: 'Edit steps', exact: true }).click();
    await expect(page.getByRole('dialog')).toBeVisible();
    await check();
  });
}
