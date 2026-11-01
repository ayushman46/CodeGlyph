import { expect, test } from '@playwright/test';
import { mockApi } from './fixtures.js';

// The first minutes with a repository: the Start here card (FEAT-14),
// consistent indexing numbers (BUG-14), the place in the indexing queue
// (FEAT-08) and a mistyped repository refused inline (UX-07). Routes added
// here take precedence over the shared mocks in fixtures.js.
test.use({ baseURL: process.env.E2E_WORKSPACE_URL || 'http://127.0.0.1:4174' });

const json = (route, body, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });

const base = {
  github_url: 'https://github.com/expressjs/express.git', owner: 'expressjs', job_status: 'completed', error_message: null,
  message: null, created_at: '2026-11-01T20:15:14Z', updated_at: '2026-11-01T20:16:34Z', embedding_status: 'complete',
  total_seen_files: 216, eligible_files: 201, excluded_files: 15, excluded_bytes: 0, excluded_reasons: {}, commit_sha: null,
  indexed_at: '2026-11-01T20:16:34Z', selection: { selected_bytes: 725710 }, refreshing: false, queue: null,
};
const express = {
  ...base, id: 'e1', repo_name: 'express', full_name: 'expressjs/express', status: 'ready', index_phase: 'ready',
  is_indexing: false, queryable: true, chunk_count: 357, semantic_progress: 100, keyword_files: 200, keyword_chunks: 357,
  indexed_files: 200, searchable_files: 200, semantic_sections: null, phase_step: 7, phase_steps: 7,
};
// The customer's moment: keyword search works for 56 files, 258 of 465 sections embedded.
const click = {
  ...base, id: 'c1', repo_name: 'click', full_name: 'pallets/click', github_url: 'https://github.com/pallets/click.git',
  status: 'searchable', index_phase: 'searchable', job_status: 'processing', is_indexing: true, queryable: true, chunk_count: 390,
  semantic_progress: 55, semantic_sections: { embedded: 258, total: 465 }, embedding_status: 'running', keyword_files: 56,
  keyword_chunks: 390, indexed_files: 0, searchable_files: 56, phase_step: 6, phase_steps: 7, indexed_at: null,
  message: 'Building the semantic index: 258 of 465 code sections (55%). Keyword search already works.',
};
const queued = {
  ...base, id: 'q1', repo_name: 'flask', full_name: 'pallets/flask', github_url: 'https://github.com/pallets/flask.git',
  status: 'queued', index_phase: 'queued', job_status: 'queued', is_indexing: true, queryable: false, chunk_count: 0,
  semantic_progress: 0, semantic_sections: null, keyword_files: 0, keyword_chunks: 0, indexed_files: 0, searchable_files: 0,
  phase_step: 1, phase_steps: 7, indexed_at: null, total_seen_files: 0, eligible_files: 0, excluded_files: 0,
  queue: {
    ahead: 2, running: 1, position: 3, estimated_wait_seconds: 75, estimate_runs: 7,
    label: '2 repositories ahead of yours · usually starts in about 2 min',
    estimate_note: 'Estimate based on how long the last 7 indexing runs took.',
  },
};

const startHere = {
  summary: { text: 'Fast, unopinionated, minimalist web framework', source: { path: 'package.json', line: 3 } },
  ecosystem: 'node',
  entry: { path: 'index.js', main: 'lib/express.js', line: 11, detail: 'index.js loads lib/express.js on line 11.', source: { path: 'index.js', line: 11 } },
  core: { path: 'lib', files: 6, detail: 'lib/express.js lives here, with 6 indexed files.', source: { path: 'index.js', line: 11 } },
  commands: [
    { kind: 'install', label: 'Install', command: 'npm install', detail: 'Installs the dependencies declared in package.json.', source: { path: 'package.json', line: 64 } },
    { kind: 'test', label: 'Run the tests', command: 'npm test', detail: 'mocha --require test/support/env --reporter spec --check-leaks test/ test/acceptance/', source: { path: 'package.json', line: 94 } },
    { kind: 'run', label: 'Run it', command: 'node examples/content-negotiation', detail: 'From the README (Examples).', source: { path: 'Readme.md', line: 146 } },
  ],
  steps: [
    { title: 'Start at the entry point', path: 'index.js', line: 11, detail: 'index.js loads lib/express.js on line 11.' },
    { title: 'Read the main module', path: 'lib/express.js', line: 36, detail: 'Defines createApplication, which index.js exports.' },
    { title: 'Then application.js', path: 'lib/application.js', line: 59, detail: 'Imported by lib/express.js on line 18; see app.init.' },
  ],
  guides: [{ title: 'Installation', path: 'Readme.md', line: 50 }, { title: 'Running Tests', path: 'Readme.md', line: 163 }],
};

const fileText = Array.from({ length: 120 }, (_, index) => `// line ${index + 1}`).join('\n');

const mockOnboarding = async (page, { ingest } = {}) => {
  await mockApi(page);
  const state = { ingests: [] };
  await page.route('**/api/**', async (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname.replace(/^\/api/, '');
    const method = route.request().method();
    if (path === '/repos' || path === '/repos/statuses') return json(route, [express, click, queued]);
    if (path === '/repos/express/overview') {
      return json(route, {
        repository: express, total_files: 200, root_files: ['History.md', 'LICENSE', 'Readme.md', 'index.js', 'package.json'],
        tree: [{ path: 'test', files: 104, bytes: 0, children: [] }, { path: 'examples', files: 80, bytes: 0, children: [] }, { path: 'lib', files: 6, bytes: 0, children: [] }],
        key_files: ['Readme.md', 'package.json', 'index.js'], languages: [{ language: 'JavaScript', files: 190, percent: 95 }],
        suggested_questions: ['What is this project, according to Readme.md?'], suggested_question_details: [], start_here: startHere, notice: null,
      });
    }
    if (path === '/repos/express/file') {
      return json(route, { path: url.searchParams.get('path'), content: fileText, source: 'github', ref: 'HEAD', language: 'javascript', line_count: 120, total_lines: 120, exact: true, github_url: null });
    }
    if (path === '/ingest' && method === 'POST') {
      state.ingests.push(route.request().postDataJSON());
      return ingest ? ingest(route) : json(route, { detail: 'Unexpected ingest' }, 500);
    }
    return route.fallback();
  });
  return state;
};

test('a new user opening Overview sees where to start, and each step opens the file at its line', async ({ page }) => {
  await mockOnboarding(page);
  await page.goto('/app/r/express');
  await page.getByRole('tab', { name: 'Overview' }).click();
  const card = page.getByRole('region', { name: 'Start here' });
  await expect(card).toBeVisible();
  await expect(card.getByText('Fast, unopinionated, minimalist web framework')).toBeVisible();
  await expect(card.getByRole('button', { name: /index\.js → lib\/express\.js/ })).toBeVisible();
  await expect(card.getByText('lib/ · 6 files', { exact: true })).toBeVisible();
  await expect(card.getByText('npm test', { exact: true })).toBeVisible();
  await expect(card.getByRole('button', { name: 'Open package.json at line 94' })).toBeVisible();
  await expect(card.getByText('npm install', { exact: true })).toBeVisible();
  await expect(card.getByRole('list', { name: 'Reading path' }).getByRole('listitem')).toHaveCount(3);

  await card.getByRole('button', { name: /Step 2: Read the main module\. Open lib\/express\.js at line 36/ }).click();
  await expect(page.getByRole('complementary', { name: 'File lib/express.js' }).first()).toBeVisible();

  // A citation opens the cited manifest line.
  await card.getByRole('button', { name: 'Open package.json at line 94' }).click();
  await expect(page.getByRole('complementary', { name: 'File package.json' }).first()).toBeVisible();
  // "Indexed files" and the folder map count the same files.
  await expect(page.getByText('200 files', { exact: true })).toBeVisible();
});

test('while the semantic index builds, the header, the step and the sidebar agree with the API', async ({ page }) => {
  await mockOnboarding(page);
  await page.goto('/app/r/click');
  // The header badge: round(258 / 465) = 55%, not the stale 66%.
  await expect(page.getByText('Ready · semantic 55%').first()).toBeVisible();
  await expect(page.getByText('258 of 465 sections embedded')).toBeVisible();
  // The sidebar shows the keyword-searchable file count, not 0.
  const entry = page.getByRole('navigation', { name: 'Repositories' }).getByRole('link', { name: /click/ });
  await expect(entry.getByText('56', { exact: true })).toBeVisible();
  // Overview: "Indexed files" reads 56 while indexing.
  await page.getByRole('tab', { name: 'Overview' }).click();
  await expect(page.getByText('56', { exact: true }).first()).toBeVisible();
});

test('a queued repository says how many are ahead and when it usually starts, as an estimate', async ({ page }) => {
  await mockOnboarding(page);
  await page.goto('/app/r/flask');
  await expect(page.getByText('step 1 of 7', { exact: false })).toBeVisible();
  await expect(page.getByText('2 repositories ahead of yours · usually starts in about 2 min')).toBeVisible();
  await expect(page.getByText('Estimate based on how long the last 7 indexing runs took.')).toBeVisible();
  // All seven steps are listed, Ready included.
  await expect(page.getByRole('list', { name: 'Indexing steps' }).getByRole('listitem')).toHaveCount(7);
});

test('a repository GitHub does not have is refused inline and never added', async ({ page }) => {
  const state = await mockOnboarding(page, {
    ingest: (route) => json(route, { detail: "Repository not found: github.com/pallets/clik-does-not-exist doesn't exist or isn't public. Check the owner and repository name." }, 404),
  });
  await page.goto('/app/r/express');
  const input = page.getByRole('textbox', { name: 'GitHub repository URL or owner/repo' });
  await input.fill('pallets/clik-does-not-exist');
  await input.press('Enter');
  await expect(page.getByRole('alert').filter({ hasText: 'Repository not found' })).toBeVisible();
  await expect(input).toHaveValue('pallets/clik-does-not-exist');
  expect(state.ingests).toEqual([{ github_url: 'pallets/clik-does-not-exist' }]);
  const sidebar = page.getByRole('navigation', { name: 'Repositories' });
  await expect(sidebar.getByRole('link')).toHaveCount(3);
  await expect(sidebar.getByText('clik-does-not-exist')).toHaveCount(0);
});
