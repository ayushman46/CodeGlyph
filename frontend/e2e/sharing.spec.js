import { expect, test } from '@playwright/test';
import { SHA, mockApi } from './fixtures.js';

// Sharing a repository with teammates (FEAT-06). The shared mocks answer
// every other /api call; the routes below are registered later, so they take
// precedence for the sharing endpoints and fall back for everything else.
test.use({ baseURL: process.env.E2E_WORKSPACE_URL || 'http://127.0.0.1:4174' });

const TOKEN = 'Jx4m8k2Q9vZpL0aT7wR3nY6uC1eB5dHfGgKiMoNsPqX';
const OWNER_LABEL = 'ann@example.com';

const repo = (overrides = {}) => ({
  id: 'r1', repo_name: 'cal.com', github_url: 'https://github.com/calcom/cal.com.git', owner: 'calcom', full_name: 'calcom/cal.com',
  status: 'ready', index_phase: 'ready', job_status: 'completed', is_indexing: false, queryable: true, chunk_count: 7421,
  error_message: null, message: null, created_at: '2026-10-01T00:00:00Z', updated_at: '2026-10-01T00:00:00Z',
  semantic_progress: 100, embedding_status: 'complete', indexed_files: 5600, commit_sha: SHA, indexed_at: '2026-10-01T00:00:00Z',
  selection: { selected_bytes: 21000000 }, refreshing: false,
  capabilities: { ask: true, search: true, browse_files: true, semantic_search: true, propose_changes: true },
  ...overrides,
});
const sharedAs = (role) => repo({
  access_role: role, shared: true, owner_label: OWNER_LABEL, owner_repo_name: 'cal.com',
  capabilities: { ask: true, search: true, browse_files: true, semantic_search: true, propose_changes: role === 'editor' },
});

const json = (route, body, status = 200, headers = {}) => route.fulfill({
  status, contentType: 'application/json', headers, body: JSON.stringify(body),
});

/** Sharing endpoints on top of mockApi; `state` records what the UI sent. */
const mockSharing = async (page, { repos = [repo({ access_role: 'owner', shared: false, member_count: 1 })], invite = {}, status } = {}) => {
  const state = {
    repos,
    created: [],
    roleChanges: [],
    removed: [],
    accepted: 0,
    left: 0,
    sharing: {
      repository: 'cal.com', full_name: 'calcom/cal.com',
      members: [{ user_id: 'u-bo', label: 'bo@example.com', role: 'viewer', joined_at: '2026-10-20T10:00:00Z', updated_at: null }],
      invites: [],
    },
  };
  await page.route('**/api/**', async (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname.replace(/^\/api/, '');
    const method = route.request().method();
    if (path === '/repos' || path === '/repos/statuses') return json(route, state.repos);
    if (path === '/repos/cal.com/sharing') return json(route, state.sharing);
    if (path === '/repos/cal.com/invites' && method === 'POST') {
      const body = route.request().postDataJSON();
      state.created.push(body);
      const created = {
        id: `inv-${state.created.length}`, role: body.role, created_at: '2026-11-02T10:00:00Z',
        expires_at: new Date(Date.now() + body.expires_in_days * 86400000).toISOString(), token_hint: TOKEN.slice(-4), accepted_count: 0, state: 'active',
      };
      return json(route, { invite: created, token: TOKEN, path: `/invite/${TOKEN}` });
    }
    if (path.startsWith('/repos/cal.com/members/') && method === 'PATCH') {
      state.roleChanges.push({ user: decodeURIComponent(path.split('/').pop()), ...route.request().postDataJSON() });
      return json(route, { member: { ...state.sharing.members[0], role: route.request().postDataJSON().role } });
    }
    if (path.startsWith('/repos/cal.com/members/') && method === 'DELETE') {
      state.removed.push(decodeURIComponent(path.split('/').pop()));
      return json(route, { removed: true });
    }
    if (path === '/repos/cal.com/membership' && method === 'DELETE') {
      state.left += 1;
      state.repos = [];
      return json(route, { left: true, repo_name: 'cal.com' });
    }
    if (path === `/invites/${TOKEN}` && method === 'GET') {
      if (invite.error) return json(route, { detail: invite.error.detail }, invite.error.status, { 'x-error-code': invite.error.code });
      return json(route, {
        status: 'valid', repository: 'cal.com', full_name: 'calcom/cal.com', owner_label: OWNER_LABEL, role: 'viewer',
        expires_at: '2026-11-09T10:00:00Z', repo_name: null, access_role: null, ...invite.preview,
      });
    }
    if (path === `/invites/${TOKEN}/accept` && method === 'POST') {
      state.accepted += 1;
      state.repos = [sharedAs('viewer')];
      return json(route, { repo_name: 'cal.com', role: 'viewer', joined: true });
    }
    if (path === '/status/cal.com' && status) return json(route, status.body, status.code, status.headers);
    if (path === '/answers/share' && method === 'POST') {
      state.answerShares.push(route.request().postDataJSON());
      return json(route, { id: 'share-1', token: TOKEN, path: `/shared/answer/${TOKEN}`, created_at: '2026-11-02T10:00:00Z' });
    }
    if (path === `/shared-answers/${TOKEN}`) {
      if (state.answerRevoked) {
        return json(route, { detail: 'This shared answer isn\'t available. The link may be incomplete, or the answer is no longer shared.' }, 404, { 'x-error-code': 'answer_unavailable' });
      }
      return json(route, SHARED_ANSWER);
    }
    if (path.startsWith('/repos/cal.com/invites/') && method === 'DELETE') {
      state.revokedInvites.push(decodeURIComponent(path.split('/').pop()));
      return json(route, { revoked: true, id: path.split('/').pop() });
    }
    if (path === '/answers/shares/share-1' && method === 'DELETE') {
      state.answerRevoked = true;
      return json(route, { revoked: true, id: 'share-1' });
    }
    return route.fallback();
  });
  state.answerShares = [];
  state.answerRevoked = false;
  state.revokedInvites = [];
  return state;
};

const SHARED_ANSWER = {
  id: 'share-1', question: 'Where is booking confirmation handled?',
  answer: '## Direct answer\nBooking confirmation is handled in `packages/features/bookings/lib/handleConfirmation.ts:L42-L118`.',
  citations: [{
    file_path: 'packages/features/bookings/lib/handleConfirmation.ts', start_line: 42, end_line: 118, language: 'ts', cited_in_answer: true,
    github_url: `https://github.com/calcom/cal.com/blob/${SHA}/packages/features/bookings/lib/handleConfirmation.ts#L42-L118`,
  }],
  repository: 'calcom/cal.com', commit_sha: SHA, answered_at: '2026-11-02T09:59:00Z', shared_by: OWNER_LABEL,
  shared_at: '2026-11-02T10:00:00Z', yours: false,
};

test('an owner creates a viewer link, changes a member to editor and removes them', async ({ page }) => {
  await mockApi(page);
  const state = await mockSharing(page);
  await page.context().grantPermissions(['clipboard-read', 'clipboard-write']);
  await page.goto('/app/r/cal.com');
  await page.getByRole('button', { name: 'Share cal.com (shared with 1)' }).click();
  const dialog = page.getByRole('dialog', { name: 'Share cal.com' });
  await expect(dialog.getByText('bo@example.com')).toBeVisible();
  await dialog.getByLabel('Link works for').selectOption('7');
  await dialog.getByRole('button', { name: 'Create link' }).click();
  await expect(dialog.getByLabel('New invite link')).toHaveValue(new RegExp(`/invite/${TOKEN}$`));
  await expect(dialog.getByText(/can join as a viewer until/)).toBeVisible();
  expect(state.created).toEqual([{ role: 'viewer', expires_in_days: 7 }]);
  await dialog.getByRole('button', { name: 'Copy link' }).click();
  await expect(page.getByLabel('Notifications').getByText('Invite link copied')).toBeVisible();
  // The new link is listed with its role, the end of its token and its expiry.
  const links = dialog.getByRole('list', { name: 'Active invite links' });
  await expect(links.getByText(`…${TOKEN.slice(-4)}`)).toBeVisible();
  await expect(links.getByText('Expires in 7 days')).toBeVisible();

  await dialog.getByLabel('Role for bo@example.com').selectOption('editor');
  await expect(page.getByLabel('Notifications').getByText('bo@example.com is now an editor')).toBeVisible();
  expect(state.roleChanges).toEqual([{ user: 'u-bo', role: 'editor' }]);

  await dialog.getByRole('button', { name: 'Remove bo@example.com' }).click();
  await dialog.getByRole('group', { name: 'Confirm removing bo@example.com' }).getByRole('button', { name: 'Remove' }).click();
  await expect(dialog.getByText('bo@example.com')).toHaveCount(0);
  expect(state.removed).toEqual(['u-bo']);
});

test('each link says who joined with it, and revoking asks first (round 2, admin)', async ({ page }) => {
  await mockApi(page);
  const state = await mockSharing(page);
  state.sharing.invites = [{
    id: 'inv-old', role: 'viewer', created_at: '2026-11-01T10:00:00Z', expires_at: new Date(Date.now() + 3 * 86400000).toISOString(),
    token_hint: 'Zq9x', accepted_count: 1, state: 'active', joined_by: [{ user_id: 'u-bo', label: 'bo@example.com' }],
  }];
  await page.goto('/app/r/cal.com');
  await page.getByRole('button', { name: 'Share cal.com (shared with 1)' }).click();
  const links = page.getByRole('dialog', { name: 'Share cal.com' }).getByRole('list', { name: 'Active invite links' });
  await expect(links.getByText('Joined with this link: bo@example.com')).toBeVisible();
  await links.getByRole('button', { name: 'Revoke the viewer link ending Zq9x' }).click();
  // Nothing is revoked until it is confirmed.
  expect(state.revokedInvites).toEqual([]);
  const confirm = links.getByRole('group', { name: 'Confirm revoking the link ending Zq9x' });
  await confirm.getByRole('button', { name: 'Cancel' }).click();
  await expect(confirm).toHaveCount(0);
  await links.getByRole('button', { name: 'Revoke the viewer link ending Zq9x' }).click();
  await links.getByRole('group', { name: 'Confirm revoking the link ending Zq9x' }).getByRole('button', { name: 'Revoke' }).click();
  await expect(page.getByLabel('Notifications').getByText('Invite link revoked')).toBeVisible();
  expect(state.revokedInvites).toEqual(['inv-old']);
  await expect(links).toHaveCount(0);
});

test('a viewer sees the repository under Shared with you, can ask, and cannot use Edit & PR or manage it', async ({ page }) => {
  await mockApi(page);
  await mockSharing(page, { repos: [sharedAs('viewer')] });
  await page.goto('/app/r/cal.com');
  const sidebar = page.getByRole('navigation', { name: 'Repositories' });
  await expect(sidebar.getByRole('heading', { name: 'Shared with you' })).toBeVisible();
  await expect(sidebar.getByRole('link', { name: `cal.com , Ready, Viewer · shared by ${OWNER_LABEL}` })).toBeVisible();

  const editMode = page.getByRole('radio', { name: 'Edit & PR' });
  await expect(editMode).toBeDisabled();
  await expect(page.getByText(/You have view access to this shared repository, so Edit & PR is off\. Ask ann@example\.com/)).toBeVisible();

  const composer = page.getByRole('textbox', { name: 'Ask about this codebase' });
  await composer.fill('Where is booking confirmation handled?');
  await composer.press('Enter');
  await expect(page.getByText('which updates the booking and sends emails.')).toBeVisible();

  await page.getByRole('button', { name: 'Repository actions' }).click();
  const menu = page.getByRole('menu');
  await expect(menu.getByRole('menuitem', { name: 'Coverage report' })).toBeVisible();
  for (const owner of ['Rename', 'Delete repository', 'Re-index…']) {
    await expect(menu.getByRole('menuitem', { name: owner })).toHaveCount(0);
  }
  await page.keyboard.press('Escape');
  await page.getByRole('button', { name: `Shared with you by ${OWNER_LABEL}: Viewer access` }).click();
  const access = page.getByRole('dialog', { name: 'Shared with you' });
  await expect(access.getByText('Need Edit & PR? Ask ann@example.com to make you an editor.')).toBeVisible();
  await access.getByRole('button', { name: 'Done' }).click();

  // The command palette offers the same actions as the menu.
  await page.keyboard.press(process.platform === 'darwin' ? 'Meta+K' : 'Control+K');
  const commands = page.getByRole('listbox', { name: 'Commands' });
  await expect(commands.getByRole('option', { name: /Coverage report/ })).toBeVisible();
  await expect(commands.getByRole('option', { name: /Rename repository|Delete repository|Re-index/ })).toHaveCount(0);
});

// The owner's first index, still running or stopped/failed, seen by `role`.
const indexing = (role, overrides = {}) => ({
  ...(role === 'owner' ? repo({ access_role: 'owner', shared: false }) : sharedAs(role)),
  status: 'chunking', index_phase: 'chunking', job_status: 'processing', is_indexing: true, queryable: false,
  chunk_count: 0, semantic_progress: 0, commit_sha: null, message: 'Reading source files (120 of 900 changed files).',
  ...overrides,
});

test('only the owner gets a Stop button while the first index runs', async ({ page }) => {
  await mockApi(page);
  const state = await mockSharing(page, { repos: [indexing('viewer')] });
  await page.goto('/app/r/cal.com');
  await expect(page.getByText('Reading source files (120 of 900 changed files).')).toBeVisible();
  await expect(page.getByRole('button', { name: 'Stop indexing cal.com' })).toHaveCount(0);
  state.repos = [indexing('owner')];
  await page.reload();
  await expect(page.getByText('Reading source files (120 of 900 changed files).')).toBeVisible();
  await expect(page.getByRole('button', { name: 'Stop indexing cal.com' })).toBeVisible();
});

test('a member of a repository whose indexing failed is told to ask the owner instead of Try again or Delete', async ({ page }) => {
  await mockApi(page);
  const failed = { status: 'failed', index_phase: 'failed', job_status: 'failed', is_indexing: false, message: 'GitHub archive download timed out.' };
  const state = await mockSharing(page, { repos: [indexing('editor', failed)] });
  await page.goto('/app/r/cal.com');
  await expect(page.getByRole('heading', { name: 'Indexing failed' })).toBeVisible();
  await expect(page.getByText('Ask ann@example.com to start indexing again. Only the owner can.')).toBeVisible();
  await expect(page.getByRole('button', { name: /Try again|Start indexing again/ })).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Delete', exact: true })).toHaveCount(0);
  await expect(page.getByText('Indexing failed. Ask ann@example.com to start it again.')).toBeVisible();
  // The owner still gets both.
  state.repos = [indexing('owner', failed)];
  await page.reload();
  await expect(page.getByRole('button', { name: 'Try again' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Delete', exact: true })).toBeVisible();
});

test('a member leaves a shared repository and is told how to come back', async ({ page }) => {
  await mockApi(page);
  const state = await mockSharing(page, { repos: [sharedAs('viewer')] });
  await page.goto('/app/r/cal.com');
  await page.getByRole('button', { name: `Shared with you by ${OWNER_LABEL}: Viewer access` }).click();
  const access = page.getByRole('dialog', { name: 'Shared with you' });
  await access.getByRole('button', { name: 'Leave…' }).click();
  await expect(access.getByText(`An invite link from ${OWNER_LABEL} that still works brings you back; any role change they made for you stays.`)).toBeVisible();
  await access.getByRole('button', { name: 'Leave repository' }).click();
  await expect(page).toHaveURL(/\/app$/);
  expect(state.left).toBe(1);
  await expect(page.getByLabel('Notifications').getByText(`To rejoin, open an invite link from ${OWNER_LABEL} that still works.`)).toBeVisible();
  await expect(page.getByRole('navigation', { name: 'Repositories' }).getByText('cal.com', { exact: true })).toHaveCount(0);
});

test('an editor can switch to Edit & PR on a shared repository', async ({ page }) => {
  await mockApi(page);
  await mockSharing(page, { repos: [sharedAs('editor')] });
  await page.goto('/app/r/cal.com');
  const editMode = page.getByRole('radio', { name: 'Edit & PR' });
  await expect(editMode).toBeEnabled();
  await editMode.click();
  await expect(page.getByRole('textbox', { name: 'Describe the code change' })).toBeVisible();
  await expect(page.locator('[data-slot="edit-disabled-reason"]')).toHaveCount(0);
});

test('opening an invite link shows the repository, owner and role, and Join opens it', async ({ page }) => {
  await mockApi(page);
  const state = await mockSharing(page, { repos: [] });
  await page.goto(`/invite/${TOKEN}`);
  await expect(page.getByRole('heading', { name: 'Join cal.com' })).toBeVisible();
  await expect(page.getByText('calcom/cal.com')).toBeVisible();
  await expect(page.getByText(OWNER_LABEL, { exact: true })).toBeVisible();
  await expect(page.getByText('Viewer', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Join' }).click();
  await expect(page).toHaveURL(/\/app\/r\/cal\.com$/);
  expect(state.accepted).toBe(1);
  await expect(page.getByRole('navigation', { name: 'Repositories' }).getByRole('heading', { name: 'Shared with you' })).toBeVisible();
});

test('a member opening a viewer link sees their own editor role, not the link\'s (round 2, admin)', async ({ page }) => {
  await mockApi(page);
  await mockSharing(page, { repos: [sharedAs('editor')], invite: { preview: { status: 'member', access_role: 'editor', repo_name: 'cal.com' } } });
  await page.goto(`/invite/${TOKEN}`);
  await expect(page.getByRole('heading', { name: 'You already have cal.com' })).toBeVisible();
  await expect(page.getByText('Your access')).toBeVisible();
  await expect(page.getByText('Editor', { exact: true })).toBeVisible();
  await expect(page.getByText('Viewer', { exact: true })).toHaveCount(0);
  await expect(page.getByText('You joined earlier as an editor.', { exact: false })).toBeVisible();
});

test('rejoining with a link older than the owner\'s role change says the owner\'s role stays', async ({ page }) => {
  await mockApi(page);
  await mockSharing(page, { repos: [], invite: { preview: { role: 'viewer', role_kept: true } } });
  await page.goto(`/invite/${TOKEN}`);
  await expect(page.getByRole('heading', { name: 'Join cal.com' })).toBeVisible();
  await expect(page.getByText('Viewer', { exact: true })).toBeVisible();
  await expect(page.getByText(`You were a member before, and ${OWNER_LABEL} changed your role after this link was made, so you rejoin as a viewer.`)).toBeVisible();
});

test('an expired or revoked invite link says so', async ({ page }) => {
  await mockApi(page);
  await mockSharing(page, {
    invite: { error: { status: 410, code: 'invite_expired', detail: `This invite link has expired. Ask ${OWNER_LABEL} for a new link.` } },
  });
  await page.goto(`/invite/${TOKEN}`);
  await expect(page.getByRole('heading', { name: 'This invite link has expired' })).toBeVisible();
  await expect(page.getByText(`Ask ${OWNER_LABEL} for a new link.`, { exact: true })).toBeVisible();
  // The heading is not repeated in the explanation under it.
  await expect(page.getByText(/This invite link has expired/)).toHaveCount(1);
  await expect(page.getByRole('button', { name: 'Join' })).toHaveCount(0);
});

test('a removed member is told the repository is no longer shared', async ({ page }) => {
  await mockApi(page);
  await mockSharing(page, {
    repos: [],
    status: { code: 404, body: { detail: 'This repository is no longer shared with you.' }, headers: { 'x-error-code': 'share_removed' } },
  });
  await page.goto('/app/r/cal.com');
  await expect(page.getByRole('heading', { name: 'This repository is no longer shared with you.' })).toBeVisible();
  await expect(page.getByRole('navigation', { name: 'Repositories' }).getByText('cal.com', { exact: true })).toHaveCount(0);
});

test('a name that was never shared still reads as not found', async ({ page }) => {
  await mockApi(page);
  await mockSharing(page, { repos: [], status: { code: 404, body: { detail: 'Repository not found.' } } });
  await page.goto('/app/r/cal.com');
  await expect(page.getByRole('heading', { name: 'Repository not found' })).toBeVisible();
});

test('Share answer makes a read-only link to the answer, and Stop sharing revokes it', async ({ page }) => {
  await mockApi(page);
  const state = await mockSharing(page);
  await page.goto('/app/r/cal.com');
  const composer = page.getByRole('textbox', { name: 'Ask about this codebase' });
  await composer.fill('Where is booking confirmation handled?');
  await composer.press('Enter');
  await expect(page.getByText('which updates the booking and sends emails.')).toBeVisible();
  await page.getByRole('button', { name: 'Share answer' }).click();
  const dialog = page.getByRole('dialog', { name: 'Share this answer' });
  await expect(dialog.getByLabel('Answer link')).toHaveValue(new RegExp(`/shared/answer/${TOKEN}$`));
  expect(state.answerShares).toHaveLength(1);
  expect(state.answerShares[0].repo_name).toBe('cal.com');
  expect(state.answerShares[0].answer).toContain('which updates the booking and sends emails.');
  await dialog.getByRole('button', { name: 'Stop sharing' }).click();
  await expect(dialog).toHaveCount(0);
  expect(state.answerRevoked).toBe(true);
});

test('a shared answer shows the question, the answer and commit-pinned sources; a revoked one says it is unavailable', async ({ page }) => {
  await mockApi(page);
  const state = await mockSharing(page);
  await page.goto(`/shared/answer/${TOKEN}`);
  await expect(page.getByRole('heading', { name: 'Where is booking confirmation handled?' })).toBeVisible();
  await expect(page.getByRole('article', { name: 'Answer' }).getByText('Booking confirmation is handled in')).toBeVisible();
  await expect(page.getByText(`Shared by ${OWNER_LABEL}`)).toBeVisible();
  const source = page.getByRole('link', { name: 'packages/features/bookings/lib/handleConfirmation.ts:42–118' });
  await expect(source).toHaveAttribute('href', new RegExp(`/blob/${SHA}/.*#L42-L118$`));
  state.answerRevoked = true;
  await page.reload();
  await expect(page.getByRole('heading', { name: 'This shared answer isn’t available' })).toBeVisible();
  await expect(page.getByText('The link may be incomplete, or the answer is no longer shared.', { exact: true })).toBeVisible();
  await expect(page.getByText(/This shared answer isn.t available/)).toHaveCount(1);
});
