import { expect, test } from '@playwright/test';
import { conversationId, mockApi, proposalReply, savedConversation } from './fixtures.js';

test.use({ baseURL: process.env.E2E_WORKSPACE_URL || 'http://127.0.0.1:4174' });

const TRIAGE = conversationId(1);
const ONBOARDING = conversationId(2);
const DAY = 86_400_000;
const triage = () => savedConversation(TRIAGE, 'Confirmation email timezone', new Date(Date.now() - DAY).toISOString(), [
  ['Why are confirmation emails sent in the wrong timezone?', 'The date is formatted in the organizer timezone.'],
  ['Send confirmation emails in the attendee timezone', '## Send confirmation emails in the attendee timezone\n\nA validated change is ready for review.', proposalReply],
]);
const onboarding = () => savedConversation(ONBOARDING, 'Where do I start?', new Date(Date.now() - 3 * DAY).toISOString(), [
  ['Where do I start?', 'Start with apps/web and packages/features.'],
]);

const historyRows = async (page) => {
  await page.getByRole('button', { name: 'Conversation history' }).click();
  return page.getByRole('dialog', { name: 'Conversations' }).getByRole('listitem');
};

test('New chat keeps the conversation; History lists both and reopens the first with its proposal', async ({ page }) => {
  const state = await mockApi(page, { conversations: [triage()] });
  await page.goto('/app/r/cal.com');
  await expect(page.getByText('Why are confirmation emails sent in the wrong timezone?')).toBeVisible();
  await expect(page).toHaveURL(new RegExp(`[?&]c=${TRIAGE}`));

  await page.getByRole('button', { name: 'New chat' }).click();
  await expect(page.getByText('What do you want to know about cal.com?')).toBeVisible();
  await expect(page.getByRole('region', { name: 'Notifications' })).toContainText('“Confirmation email timezone” is saved in History.');
  await expect(page).not.toHaveURL(/[?&]c=/);
  await expect(page.getByRole('textbox', { name: 'Ask about this codebase' })).toBeFocused();
  const composer = page.getByRole('textbox', { name: 'Ask about this codebase' });
  await composer.fill('Where is booking confirmation handled?');
  await composer.press('Enter');
  await expect(page.getByText('which updates the booking and sends emails.')).toBeVisible();
  const created = state.conversations.find((item) => item.id !== TRIAGE);
  await expect(page).toHaveURL(new RegExp(`[?&]c=${created.id}`));
  // Nothing was deleted to start the new chat.
  expect(state.conversationRequests.filter((request) => request.startsWith('DELETE'))).toEqual([]);
  expect(state.conversations.find((item) => item.id === TRIAGE).messages).toHaveLength(4);

  const rows = await historyRows(page);
  await expect(rows).toHaveCount(2);
  await expect(rows.nth(0)).toContainText('Where is booking confirmation handled?');
  await expect(rows.nth(0)).toContainText(/Today, .* · 2 messages/);
  await expect(rows.nth(0).getByRole('button', { name: /^Where is booking/ })).toHaveAttribute('aria-current', 'true');
  await expect(rows.nth(1)).toContainText('Confirmation email timezone');
  await expect(rows.nth(1)).toContainText(/Yesterday, .* · 4 messages · 1 proposal/);

  await rows.nth(1).getByRole('button', { name: /^Confirmation email timezone/ }).click();
  await expect(page.getByText('Why are confirmation emails sent in the wrong timezone?')).toBeVisible();
  await expect(page.getByText('which updates the booking and sends emails.')).toHaveCount(0);
  await expect(page).toHaveURL(new RegExp(`[?&]c=${TRIAGE}`));
  // Its Edit & PR reply still opens the proposal's diff.
  await page.locator('[data-slot="artifact-card"]').last().click();
  await expect(page.locator('#proposal-artifact-panel').getByText('const line5 = toAttendeeTimezone(5);').first()).toBeVisible();

  // A reload reopens the same conversation, and a question continues it.
  await page.reload();
  await expect(page.getByText('Why are confirmation emails sent in the wrong timezone?')).toBeVisible();
  await composer.fill('Does the change need a test?');
  await composer.press('Enter');
  await expect(page.getByText('which updates the booking and sends emails.')).toBeVisible();
  expect(state.conversations.find((item) => item.id === TRIAGE).messages).toHaveLength(6);
});

test('deleting a conversation asks first, and Undo restores it', async ({ page }) => {
  const state = await mockApi(page, { conversations: [triage(), onboarding()] });
  await page.goto(`/app/r/cal.com?c=${ONBOARDING}`);
  await expect(page.getByText('Start with apps/web and packages/features.')).toBeVisible();

  let rows = await historyRows(page);
  await expect(rows).toHaveCount(2);
  await rows.nth(0).getByRole('button', { name: 'Delete “Confirmation email timezone”' }).click();
  const dialog = page.getByRole('dialog', { name: 'Delete this conversation?' });
  await expect(dialog).toContainText('with its 4 messages and 1 proposal');
  await dialog.getByRole('button', { name: 'Cancel' }).click();
  await expect(dialog).toHaveCount(0);
  expect(state.conversations.every((item) => !item.deleted_at)).toBe(true);

  rows = await historyRows(page);
  await rows.nth(0).getByRole('button', { name: 'Delete “Confirmation email timezone”' }).click();
  await dialog.getByRole('button', { name: 'Delete' }).click();
  const toast = page.getByRole('region', { name: 'Notifications' });
  await expect(toast).toContainText('Conversation deleted');
  rows = await historyRows(page);
  await expect(rows).toHaveCount(1);
  await page.keyboard.press('Escape');
  await toast.getByRole('button', { name: 'Undo' }).click();
  await expect(toast).toContainText('Conversation restored');
  rows = await historyRows(page);
  await expect(rows).toHaveCount(2);
  await expect(rows.nth(0)).toContainText('Confirmation email timezone');
  await page.keyboard.press('Escape');

  // Deleting the open conversation leaves an empty new chat; Undo reopens it.
  rows = await historyRows(page);
  await rows.nth(1).getByRole('button', { name: 'Delete “Where do I start?”' }).click();
  await dialog.getByRole('button', { name: 'Delete' }).click();
  await expect(page.getByText('What do you want to know about cal.com?')).toBeVisible();
  await expect(page).not.toHaveURL(/[?&]c=/);
  await toast.getByRole('button', { name: 'Undo' }).click();
  await expect(page.getByText('Start with apps/web and packages/features.')).toBeVisible();
  await expect(page).toHaveURL(new RegExp(`[?&]c=${ONBOARDING}`));
});

test('a conversation deleted earlier can be restored from Recently deleted', async ({ page }) => {
  const deletedAt = new Date(Date.now() - DAY).toISOString();
  const state = await mockApi(page, { conversations: [{ ...triage(), deleted_at: deletedAt }, onboarding()] });
  await page.goto('/app/r/cal.com');
  await expect(page.getByText('Start with apps/web and packages/features.')).toBeVisible();

  const rows = await historyRows(page);
  await expect(rows).toHaveCount(1);
  const history = page.getByRole('dialog', { name: 'Conversations' });
  await history.getByRole('button', { name: 'Recently deleted' }).click();
  const deleted = history.getByRole('list', { name: 'Recently deleted conversations' });
  await expect(deleted.getByRole('listitem')).toHaveCount(1);
  await expect(deleted).toContainText('Confirmation email timezone');
  await expect(deleted).toContainText('Deleted yesterday');
  await expect(deleted).toContainText('4 messages · 1 proposal');
  expect(state.conversationRequests).toContain('GET /conversations/cal.com/threads?deleted=true&limit=20');

  await deleted.getByRole('button', { name: 'Restore “Confirmation email timezone”' }).click();
  await expect(page.getByRole('region', { name: 'Notifications' })).toContainText('Conversation restored');
  await expect(history.getByText('Nothing deleted.', { exact: false })).toBeVisible();
  expect(state.conversations.find((item) => item.id === TRIAGE).deleted_at).toBeNull();
  // Back in the list, above the older conversation, and it opens with its messages.
  const listed = history.getByRole('list', { name: 'Conversations' }).getByRole('listitem');
  await expect(listed).toHaveCount(2);
  await expect(listed.nth(0)).toContainText('Confirmation email timezone');
  await listed.nth(0).getByRole('button', { name: /^Confirmation email timezone/ }).click();
  await expect(page.getByText('Why are confirmation emails sent in the wrong timezone?')).toBeVisible();
});

test('a conversation can be renamed, and the repository reopens the one last open', async ({ page }) => {
  const state = await mockApi(page, { conversations: [triage(), onboarding()] });
  await page.goto('/app/r/cal.com');
  // The most recently active conversation opens first.
  await expect(page.getByText('Why are confirmation emails sent in the wrong timezone?')).toBeVisible();

  let rows = await historyRows(page);
  await rows.nth(1).getByRole('button', { name: 'Rename “Where do I start?”' }).click();
  const title = page.getByRole('textbox', { name: 'Conversation title' });
  await title.fill('   ');
  await page.getByRole('button', { name: 'Save' }).click();
  await expect(page.getByRole('alert').filter({ hasText: 'Enter a title.' })).toBeVisible();
  await title.fill('Onboarding  path');
  await title.press('Enter');
  await expect(rows.nth(1)).toContainText('Onboarding path');
  expect(state.conversations.find((item) => item.id === ONBOARDING).title).toBe('Onboarding path');
  await rows.nth(1).getByRole('button', { name: /^Onboarding path/ }).click();
  await expect(page.getByText('Start with apps/web and packages/features.')).toBeVisible();

  // Export takes the open conversation only, under its title.
  await page.getByRole('button', { name: 'Repository actions' }).click();
  const [download] = await Promise.all([
    page.waitForEvent('download'),
    page.getByRole('menuitem', { name: 'Export conversation' }).click(),
  ]);
  expect(download.suggestedFilename()).toBe('cal-com-onboarding-path.md');
  const exported = await (await download.createReadStream()).toArray().then((chunks) => Buffer.concat(chunks).toString('utf8'));
  expect(exported).toContain('# calcom/cal.com: Onboarding path');
  expect(exported).toContain('## Q: Where do I start?');
  expect(exported).not.toContain('wrong timezone');

  // Leaving the repository and coming back (no ?c= in the link) reopens it.
  await page.getByRole('navigation', { name: 'Repositories' }).getByText('fastapi', { exact: true }).click();
  await expect(page).toHaveURL(/\/app\/r\/fastapi$/);
  await page.getByRole('navigation', { name: 'Repositories' }).getByText('cal.com', { exact: true }).click();
  await expect(page.getByText('Start with apps/web and packages/features.')).toBeVisible();
  await expect(page).toHaveURL(new RegExp(`/app/r/cal\\.com\\?c=${ONBOARDING}$`));
  rows = await historyRows(page);
  await expect(rows.nth(1).getByRole('button', { name: /^Onboarding path/ })).toHaveAttribute('aria-current', 'true');
});

test('a link to a deleted conversation opens the latest one instead of an error', async ({ page }) => {
  await mockApi(page, { conversations: [triage()] });
  await page.goto(`/app/r/cal.com?c=${conversationId(0xdead)}`);
  await expect(page.getByText('Why are confirmation emails sent in the wrong timezone?')).toBeVisible();
  await expect(page).toHaveURL(new RegExp(`[?&]c=${TRIAGE}`));
  await expect(page.getByRole('alert')).toHaveCount(0);
});
