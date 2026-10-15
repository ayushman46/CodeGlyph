import { expect, test } from '@playwright/test';
import { mockApi } from './fixtures.js';

const OUT = process.env.SHOTS_DIR;
test.use({ baseURL: 'http://127.0.0.1:4174' });
test.skip(!OUT, 'screenshots only on demand');

for (const scheme of ['light', 'dark']) {
  test(`workspace screens ${scheme}`, async ({ page }) => {
    await page.emulateMedia({ colorScheme: scheme });
    await page.setViewportSize({ width: 1440, height: 900 });
    await mockApi(page, { githubConnected: true });
    await page.goto('/app');
    await expect(page.getByRole('navigation', { name: 'Repositories' })).toBeVisible();
    await page.screenshot({ path: `${OUT}/home-${scheme}.png` });
    await page.goto('/app/r/cal.com');
    await expect(page.getByText('How does authentication work, end to end?')).toBeVisible();
    await page.screenshot({ path: `${OUT}/repo-empty-${scheme}.png` });
    const composer = page.getByRole('textbox', { name: 'Ask about this codebase' });
    await composer.fill('Where is booking confirmation handled?');
    await composer.press('Enter');
    await expect(page.getByText('which updates the booking and sends emails.')).toBeVisible();
    await page.getByRole('button', { name: /handleConfirmation\.ts:42–118/ }).click();
    await page.waitForTimeout(600);
    await page.screenshot({ path: `${OUT}/answer-sources-${scheme}.png` });
    await page.keyboard.press('Escape');
    await page.getByRole('radio', { name: 'Edit & PR' }).click();
    const editor = page.getByRole('textbox', { name: 'Describe the code change' });
    await editor.fill('Send confirmation emails in the attendee timezone');
    await editor.press('Enter');
    await expect(page.getByText('2 files changed')).toBeVisible();
    await page.waitForTimeout(300);
    await page.screenshot({ path: `${OUT}/proposal-${scheme}.png` });
    await page.getByRole('button', { name: 'Review & create PR' }).click();
    await page.waitForTimeout(400);
    await page.screenshot({ path: `${OUT}/pr-modal-${scheme}.png` });
    await page.getByRole('radio', { name: 'Split' }).click();
    await page.screenshot({ path: `${OUT}/pr-modal-split-${scheme}.png` });
    await page.keyboard.press('Escape');
    await page.getByRole('tab', { name: 'Overview' }).click();
    await page.waitForTimeout(300);
    await page.screenshot({ path: `${OUT}/overview-${scheme}.png`, fullPage: false });
    await page.goto('/app/r/fastapi');
    await page.waitForTimeout(400);
    await page.screenshot({ path: `${OUT}/indexing-${scheme}.png` });
  });
}

test('mobile screens', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await mockApi(page, { githubConnected: true });
  await page.goto('/app/r/cal.com');
  await expect(page.getByText('How does authentication work, end to end?')).toBeVisible();
  await page.screenshot({ path: `${OUT}/mobile-repo.png` });
  const composer = page.getByRole('textbox', { name: 'Ask about this codebase' });
  await composer.fill('Where is booking confirmation handled?');
  await composer.press('Enter');
  await expect(page.getByText('which updates the booking and sends emails.')).toBeVisible();
  await page.screenshot({ path: `${OUT}/mobile-answer.png` });
  await page.getByRole('button', { name: 'Show repositories' }).click();
  await page.waitForTimeout(400);
  await page.screenshot({ path: `${OUT}/mobile-drawer.png` });
});
