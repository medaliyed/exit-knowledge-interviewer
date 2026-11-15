import { expect, test } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';
import { ids, mockWorkspace, openAccountMenu, password, signIn, workspace } from './fixtures';

const setup = '/workspace/set-password';

test('reload restores the session and the current interview without persistent browser storage', async ({ page }) => {
  const mock = await mockWorkspace(page, { approvedPlan: true });
  await signIn(page, `${workspace}/interview/${ids.session}`);
  await expect(page.getByRole('heading', { name: 'What starts the month-end reconciliation?' })).toBeVisible();
  await page.reload();
  await expect(page.getByRole('heading', { name: 'What starts the month-end reconciliation?' })).toBeVisible();
  await expect(page).toHaveURL(`${workspace}/interview/${ids.session}`);
  expect(mock.requests.filter(request => request.path === '/auth/login')).toHaveLength(1);
  expect(mock.requests.some(request => request.path === '/auth/session')).toBe(true);
  expect(await page.evaluate(() => ({ local: localStorage.length, session: sessionStorage.length }))).toEqual({ local: 0, session: 0 });
});

test('failed sign-in preserves email, clears and focuses password, and shows the retry delay', async ({ page }) => {
  await mockWorkspace(page);
  await page.goto('/');
  const email = page.getByLabel('Email', { exact: true });
  const secret = page.getByLabel('Password', { exact: true });
  await email.fill('nadia.ferrand@example.com');
  expect(await email.getAttribute('autocomplete')).toBe('username');
  expect(await secret.getAttribute('autocomplete')).toBe('current-password');
  for (let attempt = 0; attempt < 6; attempt++) {
    await secret.fill('incorrect password');
    await page.getByRole('button', { name: 'Sign in', exact: true }).click();
    await expect(page.getByRole('alert')).toContainText(attempt < 5 ? 'Email or password is incorrect.' : 'Too many attempts. Try again in 2 minutes.');
    await expect(secret).toHaveValue('');
    await expect(secret).toBeFocused();
    await expect(email).toHaveValue('nadia.ferrand@example.com');
  }
});

test('invitation token is scrubbed before use and setup validates passwords before signing in', async ({ page }) => {
  const mock = await mockWorkspace(page);
  await page.goto(`${setup}#token=valid-invitation`);
  await expect(page).toHaveURL(setup);
  await expect(page.getByRole('heading', { name: 'Set your password', exact: true })).toBeVisible();
  const newPassword = page.getByLabel('New password', { exact: true });
  const confirm = page.getByLabel('Confirm password', { exact: true });
  expect(await newPassword.getAttribute('autocomplete')).toBe('new-password');
  await newPassword.fill(password); await confirm.fill(password + '!');
  await page.getByRole('button', { name: 'Set password and sign in', exact: true }).click();
  await expect(page.getByRole('alert')).toContainText('The passwords do not match.');
  expect(mock.requests.filter(request => request.path === '/auth/setup-password')).toHaveLength(0);
  await newPassword.fill('password1234'); await confirm.fill('password1234');
  await page.getByRole('button', { name: 'Set password and sign in', exact: true }).click();
  await expect(page.getByRole('alert')).toContainText('Choose a password that is not common.');
  await newPassword.fill(password); await confirm.fill(password);
  await page.getByRole('button', { name: 'Set password and sign in', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Interview plans', exact: true })).toBeVisible();
  expect(mock.requests.find(request => request.path === '/auth/setup-password')?.body?.token).toBe('valid-invitation');
  await page.goto(`${setup}#token=valid-invitation`);
  await newPassword.fill(password); await confirm.fill(password);
  await page.getByRole('button', { name: 'Set password and sign in', exact: true }).click();
  await expect(page.getByRole('alert')).toContainText('This link is no longer valid.');
  await expect(newPassword).toHaveCount(0);
  await page.getByRole('link', { name: 'Return to sign in' }).click();
  await expect(page.getByRole('heading', { name: 'Sign in', exact: true })).toBeVisible();
});

test('missing or invalid invitation links show one generic message', async ({ page }) => {
  await mockWorkspace(page);
  await page.goto(setup);
  await expect(page.getByRole('alert')).toContainText('This link is no longer valid.');
  await expect(page.getByLabel('New password', { exact: true })).toHaveCount(0);
  await page.goto(`${setup}#token=unknown`);
  await page.getByLabel('New password', { exact: true }).fill(password);
  await page.getByLabel('Confirm password', { exact: true }).fill(password);
  await page.getByRole('button', { name: 'Set password and sign in', exact: true }).click();
  await expect(page.getByRole('alert')).toContainText('This link is no longer valid.');
  await expect(page.getByLabel('New password', { exact: true })).toHaveCount(0);
});

test('password change keeps the workspace open and signs out through the server', async ({ page }) => {
  const mock = await mockWorkspace(page);
  await signIn(page);
  await openAccountMenu(page);
  await page.getByRole('button', { name: 'Change password', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: 'Change password', exact: true });
  const fill = async (current: string) => {
    await dialog.getByLabel('Current password', { exact: true }).fill(current);
    await dialog.getByLabel('New password', { exact: true }).fill(password + '!');
    await dialog.getByLabel('Confirm password', { exact: true }).fill(password + '!');
    await dialog.getByRole('button', { name: 'Change password', exact: true }).click();
  };
  await fill('wrong password');
  await expect(dialog.getByRole('alert')).toContainText('Current password is incorrect.');
  await fill(password);
  await expect(dialog).toHaveCount(0);
  await expect(page.locator('.global-notice')).toContainText('Password changed. Other devices have been signed out.');
  await openAccountMenu(page);
  await page.getByRole('button', { name: 'Sign out', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Sign in', exact: true })).toBeVisible();
  expect(mock.requests.some(request => request.path === '/auth/logout' && request.method === 'POST')).toBe(true);
  await page.reload();
  await expect(page.getByRole('heading', { name: 'Sign in', exact: true })).toBeVisible();
});

test('session expiry clears unsent drafts and returns to the same route after sign-in', async ({ page }) => {
  const mock = await mockWorkspace(page, { approvedPlan: true });
  await signIn(page, `${workspace}/interview/${ids.session}`);
  await page.getByRole('textbox', { name: 'Your answer', exact: true }).fill('Unsent private draft');
  mock.expireSession();
  await page.getByRole('link', { name: 'Leave for now', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Sign in', exact: true })).toBeVisible();
  await expect(page.getByRole('alert')).toContainText('Your session has ended. Sign in again to reopen your saved work.');
  await page.getByLabel('Email', { exact: true }).fill('nadia.ferrand@example.com');
  await page.getByLabel('Password', { exact: true }).fill(password);
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  await expect(page).toHaveURL(`${workspace}/plans`);
  await page.getByRole('link', { name: /Continue interview/ }).click();
  await expect(page.getByRole('textbox', { name: 'Your answer', exact: true })).toHaveValue('');
});

for (const width of [360, 768, 1440]) {
  test(`password screens fit ${width}px and expose no WCAG A/AA violations`, async ({ page }) => {
    await page.setViewportSize({ width, height: 1000 });
    await mockWorkspace(page);
    const check = async () => {
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
      expect((await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa']).analyze()).violations).toEqual([]);
    };
    await page.goto(`${setup}#token=valid-invitation`);
    await expect(page.getByRole('heading', { name: 'Set your password', exact: true })).toBeVisible();
    await check();
    await page.getByLabel('New password', { exact: true }).fill(password);
    await page.getByLabel('Confirm password', { exact: true }).fill(password);
    await page.getByRole('button', { name: 'Set password and sign in', exact: true }).click();
    await expect(page.getByRole('heading', { name: 'Interview plans', exact: true })).toBeVisible();
    await openAccountMenu(page);
    await page.getByRole('button', { name: 'Change password', exact: true }).click();
    await expect(page.getByRole('dialog')).toBeVisible();
    await check();
  });
}
