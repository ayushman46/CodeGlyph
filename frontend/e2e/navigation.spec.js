import { expect, test } from '@playwright/test';
import { CITED_PATH, READY_REPO, mockApi } from './fixtures.js';

// Finding and opening code: exact search, ⌘K files and symbols, @ mentions,
// the file tree, links to a file and line, the outline and Markdown files.
test.use({ baseURL: process.env.E2E_WORKSPACE_URL || 'http://127.0.0.1:4174' });

const json = (body) => ({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });
const viewer = (page, path = CITED_PATH) => page.getByRole('complementary', { name: `File ${path}` }).first();
const row = (page, line) => page.locator(`tr[data-line="${line}"]`).first();
const SEARCH = {
  query: 'RequestField', searchable: true, mode: 'exact', more_files: false, line_count: 3,
  results: [{
    file_path: CITED_PATH, start_line: 34, end_line: 120, language: 'ts', match_count: 3, definitions: 0, truncated: false,
    matches: [
      { line: 34, text: 'import { RequestField } from "./fields";', spans: [[9, 21]], definition: false },
      { line: 99, text: 'const fields: RequestField[] = [];', spans: [[14, 26]], definition: false },
      { line: 120, text: 'const rf = new RequestField(name);', spans: [[15, 27]], definition: false },
    ],
  }],
};

test('code search lists each file once and opens the line that was clicked (BUG-03, BUG-12)', async ({ page }) => {
  await mockApi(page);
  await page.route('**/api/repos/cal.com/search**', (route) => route.fulfill(json(SEARCH)));
  await page.goto('/app/r/cal.com?tab=overview');
  await page.getByPlaceholder(/Search code/).fill('RequestField');
  await expect(page.getByTestId('code-search-summary')).toHaveText('1 file · 3 matching lines');
  const files = page.getByRole('list', { name: 'Matching files' });
  await expect(files.locator(':scope > li')).toHaveCount(1);
  await expect(files.locator('mark').first()).toHaveText('RequestField');
  // The third line, not the file's first match, opens.
  await files.locator('button[data-line="120"]').click();
  const panel = viewer(page);
  await expect(panel.getByText('Line 120', { exact: true })).toBeVisible();
  await expect(row(page, 120)).toHaveClass(/color-highlight/);
  await expect(row(page, 34)).not.toHaveClass(/color-highlight/);
  await expect(page).toHaveURL(/tab=overview&file=packages\/features\/bookings\/lib\/handleConfirmation\.ts&line=120$/);
});

test('several words that never meet on one line say so (BUG-03)', async ({ page }) => {
  await mockApi(page);
  await page.route('**/api/repos/cal.com/search**', (route) => route.fulfill(json({ ...SEARCH, mode: 'all_words' })));
  await page.goto('/app/r/cal.com?tab=overview');
  await page.getByPlaceholder(/Search code/).fill('fields RequestField');
  await expect(page.getByText('No line has these words together, so these lines contain each of them.')).toBeVisible();
});

test('⌘K jumps to a declaration at its exact line and to a file by name (FEAT-01)', async ({ page }) => {
  await mockApi(page);
  await page.goto('/app/r/cal.com');
  await expect(page.getByRole('textbox', { name: 'Ask about this codebase' })).toBeVisible();
  await page.keyboard.press(process.platform === 'darwin' ? 'Meta+K' : 'Control+K');
  const box = page.getByRole('combobox', { name: 'Search commands' });
  await box.fill('handleConfirmation');
  const options = page.getByRole('listbox', { name: 'Commands' }).getByRole('option');
  await expect(options.first()).toContainText('function handleConfirmation');
  await expect(options.first()).toContainText(`${CITED_PATH}:42`);
  await page.keyboard.press('Enter');
  await expect(viewer(page).getByText('Line 42', { exact: true })).toBeVisible();
  await expect(row(page, 42)).toHaveClass(/color-highlight/);
  await expect(page).toHaveURL(/file=packages\/features\/bookings\/lib\/handleConfirmation\.ts&line=42$/);

  await page.keyboard.press(process.platform === 'darwin' ? 'Meta+K' : 'Control+K');
  await box.fill('getBooking.ts');
  await expect(options.first()).toContainText('packages/features/bookings/lib/getBooking.ts');
  await page.keyboard.press('Enter');
  await expect(viewer(page, 'packages/features/bookings/lib/getBooking.ts')).toBeVisible();
});

test.describe('on a phone', () => {
  test.use({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true });

  test('a header button opens the palette, which has no keyboard shortcut there (R2-02)', async ({ page }) => {
    await mockApi(page);
    await page.goto('/app/r/cal.com');
    await expect(page.getByRole('textbox', { name: 'Ask about this codebase' })).toBeVisible();
    const open = page.getByRole('button', { name: 'Open command palette' });
    await expect(open).toBeVisible();
    const size = await open.boundingBox();
    expect(size.width).toBeGreaterThanOrEqual(44);
    expect(size.height).toBeGreaterThanOrEqual(44);
    await open.tap();
    const box = page.getByRole('combobox', { name: 'Search commands' });
    await box.fill('handleConfirmation');
    const options = page.getByRole('listbox', { name: 'Commands' }).getByRole('option');
    await expect(options.first()).toContainText(`${CITED_PATH}:42`);
    await options.first().tap();
    await expect(viewer(page).getByText('Line 42', { exact: true })).toBeVisible();
  });
});

test('@ suggests a file by its own name and by a partial path (BUG-11)', async ({ page }) => {
  await mockApi(page);
  const asked = [];
  page.on('request', (request) => { if (request.url().includes('/files?')) asked.push(new URL(request.url()).searchParams.get('q')); });
  await page.goto('/app/r/cal.com');
  const composer = page.getByRole('textbox', { name: 'Ask about this codebase' });
  await composer.click();
  await composer.pressSequentially('Explain @getBook');
  const suggestions = page.getByRole('listbox', { name: 'Indexed files' });
  await expect(suggestions.getByRole('option').first()).toHaveText('packages/features/bookings/lib/getBooking.ts');
  await composer.fill('');
  await composer.pressSequentially('Explain @packages/lib/ti');
  await expect(suggestions.getByRole('option').first()).toHaveText('packages/lib/timezone.ts');
  expect(asked).toContain('packages/lib/ti');
});

test('the file tree lists source folders first and opens a file (FEAT-03)', async ({ page }) => {
  await mockApi(page);
  await page.goto('/app/r/cal.com?tab=overview');
  const tree = page.getByRole('list', { name: 'Indexed files' });
  const folders = tree.locator(':scope > li > button[aria-expanded]');
  await expect(folders).toHaveText([/^apps\//, /^packages\//, /^\.github\//]);
  await tree.getByRole('button', { name: /^packages\// }).click();
  await tree.getByRole('button', { name: /^features\// }).click();
  await tree.getByRole('button', { name: /^bookings\// }).click();
  await tree.getByRole('button', { name: 'lib/ , 2 files' }).click();
  await tree.getByRole('button', { name: 'getBooking.ts' }).click();
  await expect(viewer(page, 'packages/features/bookings/lib/getBooking.ts')).toBeVisible();
  await expect(page).toHaveURL(/tab=overview&file=packages\/features\/bookings\/lib\/getBooking\.ts$/);
});

test('a link to a file and line reopens the viewer there, and selecting a line updates it (FEAT-10)', async ({ page }) => {
  await mockApi(page);
  await page.goto(`/app/r/cal.com?file=${CITED_PATH}&line=42`);
  const panel = viewer(page);
  await expect(panel.getByText('Line 42', { exact: true })).toBeVisible();
  await expect(row(page, 42)).toHaveClass(/color-highlight/);
  await panel.getByPlaceholder('Go to line').fill('77');
  await panel.getByPlaceholder('Go to line').press('Enter');
  await expect(page).toHaveURL(/line=77$/);
  // Switching tabs keeps the link; closing the viewer drops it.
  await page.getByRole('tab', { name: 'Overview' }).click();
  await expect(page).toHaveURL(/tab=overview&file=.*&line=77$/);
  await panel.getByRole('button', { name: 'Close' }).click();
  await expect(page).toHaveURL(/\/app\/r\/cal\.com\?tab=overview$/);
});

test('a link opened while the repository is still indexing opens once it can be searched (FEAT-10)', async ({ page }) => {
  await mockApi(page);
  // fastapi is indexing when the page loads; the second status poll (polls run every 3 s) finds it searchable.
  const ready = {
    ...READY_REPO, id: 'r2', repo_name: 'fastapi', github_url: 'https://github.com/tiangolo/fastapi.git', owner: 'tiangolo',
    full_name: 'tiangolo/fastapi', commit_sha: 'b'.repeat(40),
  };
  const indexing = {
    ...ready, status: 'chunking', index_phase: 'chunking', job_status: 'processing', is_indexing: true, queryable: false,
    chunk_count: 0, semantic_progress: 0, commit_sha: null, message: 'Reading source files (120 of 900 changed files).', selection: {},
  };
  let polls = 0;
  await page.route('**/api/repos', (route) => route.fulfill(json([indexing])));
  await page.route('**/api/repos/statuses', (route) => {
    polls += 1;
    return route.fulfill(json([polls >= 2 ? ready : indexing]));
  });
  await page.route('**/api/repos/fastapi/overview', (route) => route.fulfill(json({
    repository: ready, total_files: 900, tree: [], root_files: [], key_files: [], languages: [], suggested_questions: [],
  })));
  await page.route((url) => url.pathname === '/api/repos/fastapi/file', (route) => route.fulfill(json({
    path: 'fastapi/routing.py', content: Array.from({ length: 40 }, (_, index) => `value_${index + 1} = ${index + 1}`).join('\n'),
    source: 'github', ref: 'b'.repeat(40), language: 'python', line_count: 40, total_lines: 40, exact: true, github_url: null, outline: [],
  })));
  await page.goto('/app/r/fastapi?file=fastapi/routing.py&line=12');
  const panel = viewer(page, 'fastapi/routing.py');
  await expect(panel.getByText('Line 12', { exact: true })).toBeVisible({ timeout: 15_000 });
  await expect(row(page, 12)).toHaveClass(/color-highlight/);
  // Loading the conversation that comes with being searchable keeps the viewer and the link.
  await page.waitForTimeout(1000);
  await expect(panel).toBeVisible();
  await expect(page).toHaveURL(/\/app\/r\/fastapi\?file=fastapi\/routing\.py&line=12$/);
});

test('an empty file opens as empty, and the tree says so', async ({ page }) => {
  await mockApi(page);
  await page.route('**/api/repos/cal.com/files**', (route) => route.fulfill(json({
    path: '', directories: [], files: [{ path: '__init__.py', name: '__init__.py', bytes: 0 }, { path: 'package.json', name: 'package.json', bytes: 100 }], total_files: 2,
  })));
  await page.route((url) => url.pathname === '/api/repos/cal.com/file', (route) => route.fulfill(json({
    path: '__init__.py', content: '', source: 'index', ref: 'a'.repeat(40), language: 'python', line_count: 0, total_lines: 0,
    exact: true, github_url: null, outline: [],
  })));
  await page.goto('/app/r/cal.com?tab=overview');
  const tree = page.getByRole('list', { name: 'Indexed files' });
  await expect(tree.getByRole('button', { name: '__init__.py , empty' })).toBeVisible();
  await tree.getByRole('button', { name: '__init__.py , empty' }).click();
  const panel = viewer(page, '__init__.py');
  await expect(panel.getByText('This file is empty.')).toBeVisible();
  await expect(panel.getByText('From the index')).toHaveCount(0);
  await expect(panel.getByText(/^\d+ lines?$/)).toHaveCount(0);
});

test('the file tree reloads its open folders when the index changes (FEAT-03)', async ({ page }) => {
  await mockApi(page);
  let refreshed = false;
  // fastapi (mocked as indexing) keeps the status poll running every 3 s.
  await page.route('**/api/repos/statuses', (route) => route.fulfill(json([
    refreshed ? { ...READY_REPO, commit_sha: 'c'.repeat(40), indexed_at: '2026-10-03T00:00:00Z' } : READY_REPO,
  ])));
  await page.route('**/api/repos/cal.com/files**', (route) => {
    const dir = new URL(route.request().url()).searchParams.get('dir') || '';
    if (!dir) return route.fulfill(json({ path: '', directories: [{ path: 'lib', name: 'lib', files: refreshed ? 1 : 2 }], files: [], total_files: 2 }));
    const files = refreshed ? ['lib/new.ts'] : ['lib/old.ts', 'lib/removed.ts'];
    return route.fulfill(json({ path: 'lib', directories: [], files: files.map((path) => ({ path, name: path.slice(4), bytes: 10 })), total_files: files.length }));
  });
  await page.goto('/app/r/cal.com?tab=overview');
  const tree = page.getByRole('list', { name: 'Indexed files' });
  await tree.getByRole('button', { name: /^lib\// }).click();
  await expect(tree.getByRole('button', { name: 'removed.ts' })).toBeVisible();
  // A refresh finishes: the next status poll reports a new commit.
  refreshed = true;
  await expect(tree.getByRole('button', { name: 'new.ts' })).toBeVisible({ timeout: 15_000 });
  await expect(tree.getByRole('button', { name: 'removed.ts' })).toHaveCount(0);
  // The folder stayed open.
  await expect(tree.getByRole('button', { name: /^lib\// })).toHaveAttribute('aria-expanded', 'true');
});

test('the viewer names the enclosing declaration and lists an outline (FEAT-02)', async ({ page }) => {
  await mockApi(page);
  await page.goto(`/app/r/cal.com?file=${CITED_PATH}&line=60`);
  const panel = viewer(page);
  await expect(panel.getByRole('navigation', { name: 'Line 60 is inside' })).toHaveText('function handleConfirmation');
  await panel.getByRole('button', { name: 'Outline' }).click();
  await panel.getByRole('list', { name: 'Outline' }).getByRole('button', { name: /function handleConfirmation/ }).click();
  await expect(page).toHaveURL(/line=42$/);
});

test('Markdown opens rendered, with the source a click away (UX-04)', async ({ page }) => {
  await mockApi(page);
  await page.route('**/api/repos/cal.com/file**', (route) => route.fulfill(json({
    path: 'README.md', content: '# Cal.com\n\nScheduling infrastructure.\n\n## Setup\n\nSee [the docs](docs/setup.md).\n', source: 'github',
    ref: 'a'.repeat(40), language: 'markdown', line_count: 7, total_lines: 7, exact: true, github_url: null,
    outline: [{ name: 'Cal.com', kind: 'h1', line: 1, end_line: 7, depth: 0 }, { name: 'Setup', kind: 'h2', line: 5, end_line: 7, depth: 1 }],
  })));
  await page.goto('/app/r/cal.com?file=README.md');
  const panel = viewer(page, 'README.md');
  await expect(panel.getByRole('heading', { name: 'Setup' })).toBeVisible();
  await expect(panel.getByRole('radio', { name: 'Rendered' })).toHaveAttribute('aria-checked', 'true');
  await panel.getByRole('radio', { name: 'Source' }).click();
  await expect(row(page, 5)).toContainText('## Setup');
});
