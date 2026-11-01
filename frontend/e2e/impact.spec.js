import { expect, test } from '@playwright/test';
import { CITED_PATH, SHA, mockApi } from './fixtures.js';

test.use({ baseURL: process.env.E2E_WORKSPACE_URL || 'http://127.0.0.1:4174', permissions: ['clipboard-read', 'clipboard-write'] });

const GITHUB_URL = 'https://github.com/calcom/cal.com.git';
const METHOD = 'Static analysis of resolved imports in the indexed code.';

const base = { repository: 'cal.com', commit_sha: SHA, github_url: GITHUB_URL, graph: { version: 2, rebuilt: false, rebuilding: false } };

const ambiguous = {
  ...base, query: 'app.py', ambiguous: true, candidate_count: 3, target_file: null, target_files: [], dependents: [],
  candidates: [
    { file_path: 'src/flask/app.py', dependent_count: 6 },
    { file_path: 'src/flask/sansio/app.py', dependent_count: 7 },
    { file_path: 'tests/test_apps/cliapp/app.py', dependent_count: 1 },
  ],
  summary: '3 indexed files match app.py. Choose one to see its impact; nothing is analysed until you do.',
};

const direct = {
  ...base, query: 'src/flask/app.py', ambiguous: false, candidates: [], target_file: 'src/flask/app.py', target_files: ['src/flask/app.py'],
  depth: 1, depth_counts: { 1: 2 }, total_dependents: 2, indirect_beyond_depth: 30,
  dependents: [
    { file_path: 'src/flask/__init__.py', depth: 1, via: 'src/flask/app.py', line_number: 2, import_name: '.app', is_test: false },
    { file_path: 'src/flask/testing.py', depth: 1, via: 'src/flask/app.py', line_number: 24, import_name: '.app', is_test: false },
  ],
  dependent_files: ['src/flask/__init__.py', 'src/flask/testing.py'], dependent_count: 2,
  imports: [{ file_path: 'src/flask/ctx.py', line_number: 32, import_name: '.ctx' }], imported_files: ['src/flask/ctx.py'],
  affected_tests: { files: [], count: 0, truncated: false, indirect_count: 27, functions: [], functions_truncated: false, scanned_files: 0 },
  symbol: null,
  summary: '2 indexed files import src/flask/app.py directly. 30 more files import it indirectly (within 3 levels).',
  method: METHOD,
};

const deep = {
  ...direct, depth: 3, depth_counts: { 1: 2, 2: 2 }, total_dependents: 4, indirect_beyond_depth: 0,
  dependents: [
    ...direct.dependents,
    { file_path: 'tests/conftest.py', depth: 2, via: 'src/flask/__init__.py', line_number: 6, import_name: 'flask', is_test: false },
    { file_path: 'tests/test_basic.py', depth: 2, via: 'src/flask/__init__.py', line_number: 24, import_name: 'flask', is_test: true },
  ],
  affected_tests: {
    files: [{ file_path: 'tests/test_basic.py', depth: 2, via: 'src/flask/__init__.py', line_number: 24 }],
    count: 1, truncated: false, indirect_count: 0, scanned_files: 1, functions_truncated: false,
    functions: [
      { file_path: 'tests/test_basic.py', name: 'test_make_response', line: 128, kind: 'reference', reference_line: 129, references: 4 },
      { file_path: 'tests/test_basic.py', name: 'test_response_types', line: 114, kind: 'name', matched: 'response', reference_line: null, references: 0 },
    ],
  },
  symbol: { name: 'make_response', defined_at: 40, mentions: 3 },
  summary: '2 indexed files import src/flask/app.py directly, and 2 more files reach it through them (up to 3 levels). 1 test file is affected.',
};

const mockImpact = async (page) => {
  const requests = [];
  await page.route('**/api/repos/cal.com/impact/suggestions', (route) => route.fulfill({
    status: 200, contentType: 'application/json',
    body: JSON.stringify({ repository: 'cal.com', files: [{ file_path: 'src/flask/__init__.py', dependent_count: 48 }], graph: base.graph }),
  }));
  await page.route((url) => url.pathname === '/api/repos/cal.com/impact', (route) => {
    const params = Object.fromEntries(new URL(route.request().url()).searchParams);
    requests.push(params);
    let body = direct;
    if (params.file_path === 'app.py') body = ambiguous;
    else if (params.depth === '3') body = deep;
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });
  });
  return requests;
};

const openImpact = async (page) => {
  await page.getByRole('button', { name: 'Repository actions' }).click();
  await page.getByRole('menuitem', { name: 'Change impact' }).click();
  return page.getByRole('dialog', { name: 'Change impact' });
};

test('an ambiguous file name shows a picker and analyses nothing until a file is chosen', async ({ page }) => {
  await mockApi(page);
  const requests = await mockImpact(page);
  await page.goto('/app/r/cal.com');
  const dialog = await openImpact(page);
  // The example comes from this repository, not a hard-coded TypeScript path.
  await expect(dialog.getByLabel('File')).toHaveAttribute('placeholder', 'src/flask/__init__.py');
  await dialog.getByLabel('File').fill('app.py');
  await dialog.getByRole('button', { name: 'Analyze' }).click();
  const picker = dialog.getByRole('list', { name: 'Files matching app.py' });
  await expect(picker.getByRole('button')).toHaveCount(3);
  await expect(dialog.getByRole('region', { name: 'Imported by' })).toHaveCount(0);
  await picker.getByRole('button', { name: /^src\/flask\/app\.py/ }).click();
  await expect(dialog.getByRole('region', { name: 'Imported by' })).toBeVisible();
  expect(requests.map((request) => request.file_path)).toEqual(['app.py', 'src/flask/app.py']);
});

test('a dependent opens at its import line inside the dialog and Back returns to the list', async ({ page }) => {
  await mockApi(page);
  await mockImpact(page);
  await page.goto('/app/r/cal.com');
  const dialog = await openImpact(page);
  await dialog.getByLabel('File').fill('src/flask/app.py');
  await dialog.getByRole('button', { name: 'Analyze' }).click();
  const row = dialog.getByRole('button', { name: /src\/flask\/testing\.py:24/ });
  await row.click();
  await expect(dialog.getByText('src/flask/testing.py imports src/flask/app.py on line 24.')).toBeVisible();
  const viewer = dialog.locator('[data-slot="artifact-panel"]');
  await expect(viewer.getByText('src/flask · lines 24')).toBeVisible();
  await expect(viewer.locator('tr[data-line="24"]')).toHaveClass(/highlight/);
  const back = dialog.getByRole('button', { name: 'Back to impact' });
  await expect(back).toBeFocused();
  await back.click();
  await expect(row).toBeFocused();
  await expect(dialog.getByRole('region', { name: 'Imported by' })).toBeVisible();
  // Escape inside a file goes back; a second Escape closes the dialog.
  await row.click();
  await page.keyboard.press('Escape');
  await expect(row).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(dialog).toHaveCount(0);
});

test('indirect dependents are grouped by depth with the affected tests and test functions for a symbol', async ({ page }) => {
  await mockApi(page);
  const requests = await mockImpact(page);
  await page.goto('/app/r/cal.com');
  const dialog = await openImpact(page);
  await dialog.getByLabel('File').fill('src/flask/app.py');
  await dialog.getByRole('button', { name: 'Analyze' }).click();
  const tests = dialog.getByRole('region', { name: 'Affected tests' });
  await expect(tests.getByText('Affected tests (0 direct, 27 indirect)')).toBeVisible();
  await dialog.getByLabel(/Symbol/).fill('make_response');
  await tests.getByRole('button', { name: /Include the 27 test files/ }).click();
  await expect(dialog.getByLabel(/Include indirect/)).toBeChecked();
  await expect(dialog.getByRole('region', { name: 'Imported by' }).locator('summary', { hasText: '2 levels away (2)' })).toBeVisible();
  expect(requests.at(-1)).toMatchObject({ file_path: 'src/flask/app.py', depth: '3', symbol: 'make_response' });
  await expect(tests.getByText('Static analysis')).toBeVisible();
  await expect(tests.getByText(/not by running tests, so this is not coverage/)).toBeVisible();
  await expect(tests.getByRole('button', { name: /test_make_response:128 uses make_response at line 129 \(4 times\)/ })).toBeVisible();
  await tests.getByRole('button', { name: /test_response_types:114 name contains “response”; does not mention make_response/ }).click();
  await expect(dialog.getByText('test_response_types, line 114: name contains “response”; does not mention make_response.')).toBeVisible();
  await expect(dialog.locator('tr[data-line="114"]')).toHaveClass(/highlight/);
  await dialog.getByRole('button', { name: 'Back to impact' }).click();
  await dialog.getByRole('button', { name: 'Copy as Markdown' }).click();
  await expect(dialog.getByRole('button', { name: 'Copied' })).toBeVisible();
  const markdown = await page.evaluate(() => navigator.clipboard.readText());
  expect(markdown).toContain('## Change impact: `src/flask/app.py`');
  expect(markdown).toContain(`https://github.com/calcom/cal.com/blob/${SHA}/tests/test_basic.py#L114`);
  expect(markdown).toContain('**2 levels away (2)**');
});

test('opening Change impact while a file is open prefills its path', async ({ page }) => {
  await mockApi(page);
  await mockImpact(page);
  await page.goto('/app/r/cal.com');
  const composer = page.getByRole('textbox', { name: 'Ask about this codebase' });
  await composer.fill('Where is booking confirmation handled?');
  await composer.press('Enter');
  await page.getByRole('button', { name: /handleConfirmation\.ts:42–118/ }).click();
  await expect(page.getByRole('complementary', { name: `File ${CITED_PATH}` }).first()).toBeVisible();
  const dialog = await openImpact(page);
  await expect(dialog.getByLabel('File')).toHaveValue(CITED_PATH);
});
