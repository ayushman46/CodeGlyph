import { expect, test } from '@playwright/test';
import { CITED_PATH, SHA, mockApi } from './fixtures.js';

// Answer trust: cut-off answers with Continue, code names and references the
// index does not back, named model failures, and copying with permalinks.

test.use({ baseURL: process.env.E2E_WORKSPACE_URL || 'http://127.0.0.1:4174' });

const GITHUB_URL = 'https://github.com/calcom/cal.com.git';
const snapshot = { commit_sha: SHA, github_url: GITHUB_URL };
const sse = (events) => `: connected\n\n${events.map(([event, data]) => `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`).join('')}`;
const verified = { text: `${CITED_PATH}:L42-L118`, file_path: CITED_PATH, start_line: 42, end_line: 118, valid: true };
const done = (fields) => ({
  mode: 'rag', citations: [], tool_calls: [], latency_ms: 900, model_profile: 'fast', workflow: 'general', evidence_plan: {},
  edit_suggestion: null, edit_ticket: null, snapshot, answer_references: [verified], unverified_identifiers: [],
  answer_failure: null, answer_notice: null, message_id: 'stored-1', truncated: false, ...fields,
});

/** Answers every POST /query/stream with `handler(payload)` (an SSE event list) and records the payloads. */
const routeStream = async (page, handler) => {
  const payloads = [];
  await page.route('**/api/query/stream', (route) => {
    const payload = route.request().postDataJSON();
    payloads.push(payload);
    return route.fulfill({ status: 200, headers: { 'content-type': 'text/event-stream' }, body: sse(handler(payload)) });
  });
  return payloads;
};

const ask = async (page, question = 'How does booking confirmation work?') => {
  const composer = page.getByRole('textbox', { name: 'Ask about this codebase' });
  await composer.fill(question);
  await composer.press('Enter');
};

const CUT = `## Direct answer\nConfirmation starts in \`${CITED_PATH}:L42-L118\`. The handler then emails the attendee; otherwise it`;
const BASE = `## Direct answer\nConfirmation starts in \`${CITED_PATH}:L42-L118\`. `;

test('a cut-off answer says so, Continue appends the rest, and the flag survives a reload', async ({ page }) => {
  await mockApi(page);
  const payloads = await routeStream(page, (payload) => (payload.continue_from ? [
    ['stage', { stage: 'generating', detail: 'Writing the rest of the answer' }],
    ['continue', { base: BASE, message_id: 'stored-1' }],
    ['token', { text: 'The handler then emails the attendee and marks the booking accepted.' }],
    ['done', done({ answer: `${BASE}The handler then emails the attendee and marks the booking accepted.`, continued: true })],
  ] : [
    ['token', { text: CUT }],
    ['done', done({ answer: CUT, mode: 'truncated', truncated: true })],
  ]));
  await page.goto('/app/r/cal.com');
  await ask(page);

  const notice = page.getByText('This answer reached the length limit');
  await expect(notice).toBeVisible();
  const check = page.locator('[data-slot="answer-check"]').last();
  await expect(check).toContainText('So far, 1 file/line reference exists in the indexed code');
  await expect(page.getByText(/were checked against the indexed code/)).toHaveCount(0);
  await expect(page.getByText('Follow-ups')).toHaveCount(0);

  await page.getByRole('button', { name: 'Continue' }).click();
  await expect(page.getByText('marks the booking accepted.')).toBeVisible();
  await expect(notice).toHaveCount(0);
  await expect(page.getByText('otherwise it')).toHaveCount(0);
  await expect(check).toContainText('1 file/line reference exists in the indexed code');
  await expect(check).not.toContainText('So far');
  expect(payloads[1]).toMatchObject({ continue_from: 'stored-1', repo_name: 'cal.com', model_profile: 'fast' });

  // Stored as truncated: a reload shows the notice again (until continued).
  await page.route('**/api/conversations/**', (route) => route.fulfill({
    status: 200, contentType: 'application/json',
    body: JSON.stringify({
      has_more: false, next_before: null,
      messages: [
        { id: 'q-1', role: 'user', content: 'How does booking confirmation work?', citations: [], tool_calls: [], created_at: '2026-11-01T00:00:00Z' },
        { id: 'stored-1', role: 'assistant', content: CUT, mode: 'truncated', citations: [], tool_calls: [], answer_references: [verified], created_at: '2026-11-01T00:00:01Z' },
      ],
    }),
  }));
  await page.reload();
  await expect(page.getByText('This answer reached the length limit')).toBeVisible();
  await expect(page.getByRole('button', { name: 'Continue' })).toBeEnabled();
});

test('a failed continuation keeps the answer and says why', async ({ page }) => {
  await mockApi(page);
  await routeStream(page, (payload) => (payload.continue_from ? [
    ['continue', { base: BASE, message_id: 'stored-1' }],
    ['error', { detail: 'The rest of the answer could not be written. The Fast model did not respond in time. The answer so far is unchanged.', status: 503 }],
  ] : [['done', done({ answer: CUT, mode: 'truncated', truncated: true })]]));
  await page.goto('/app/r/cal.com');
  await ask(page);
  await page.getByRole('button', { name: 'Continue' }).click();
  await expect(page.getByRole('alert').filter({ hasText: 'The Fast model did not respond in time.' })).toBeVisible();
  await expect(page.getByText('otherwise it')).toBeVisible();
  await expect(page.getByText('This answer reached the length limit')).toBeVisible();
});

test('code names and references the index does not back are flagged with the actual reason', async ({ page }) => {
  await mockApi(page);
  const answer = [
    '## Direct answer',
    `\`_set_timeout\` runs in \`${CITED_PATH}:L42-L118\` inside \`BaseClient.send_request\`.`,
    'Config lives in `package.json:L92-L98`; views in `view.js`; and `lib/gone.js:L3`.',
  ].join('\n');
  await routeStream(page, () => [['done', done({
    answer,
    unverified_identifiers: [{ text: 'BaseClient.send_request', names: ['send_request'] }],
    answer_references: [
      verified,
      { text: 'package.json:L92-L98', file_path: 'package.json', start_line: 92, end_line: 98, valid: false, reason: 'range_outside_evidence' },
      { text: 'view.js', file_path: null, start_line: null, end_line: null, valid: false, reason: 'ambiguous', candidates: ['lib/view.js', 'test/view.js'] },
      { text: 'lib/gone.js:L3', file_path: null, start_line: 3, end_line: 3, valid: false, reason: 'not_in_index' },
    ],
  })]]);
  // The search matches prefixes, as the real one does: only a longer name comes back.
  await page.route('**/api/repos/cal.com/search**', (route) => route.fulfill({
    status: 200, contentType: 'application/json', body: JSON.stringify({ query: 'send_request', results: [
      { file_path: 'docs/advanced/extensions.md', start_line: 1, end_line: 2, matches: [{ line: 1, text: 'Calls `http11.send_request_headers` during a request.' }] },
    ] }),
  }));
  await page.goto('/app/r/cal.com');
  await ask(page, 'List every call site of _set_timeout and show its definition.');

  const prose = page.locator('.prose-answer').last();
  await expect(prose.locator('code[data-identifier-status="not-found"]', { hasText: 'BaseClient.send_request' })).toBeVisible();
  await expect(prose.locator('code[data-identifier-status="not-found"]')).toHaveCount(1);
  const check = page.locator('[data-slot="answer-check"]').last();
  await expect(check).toContainText('2 of 4 file/line references exist in the indexed code');
  await expect(check).toContainText('3 references and 1 code name need a second look');
  await expect(check.locator('li', { hasText: 'BaseClient.send_request' })).toContainText('not found in the indexed code');
  await expect(check.locator('li', { hasText: 'package.json:L92-L98' })).toContainText('exists in the index but was not part of this answer’s evidence');
  await expect(check.locator('li', { hasText: 'view.js' })).toContainText('matches several files');
  await expect(check.getByRole('button', { name: 'test/view.js' })).toBeVisible();
  await expect(check.locator('li', { hasText: 'lib/gone.js:L3' })).toContainText('not in the indexed snapshot');
  await expect(check.locator('li', { hasText: 'package.json:L92-L98' })).not.toContainText('not in the indexed snapshot');

  // The out-of-evidence reference is still a link to the indexed file.
  await prose.getByRole('button', { name: /package\.json:92–98 \(exists in the index but was not part of this answer’s evidence\)/ }).click();
  await expect(page.getByRole('complementary', { name: 'File package.json' }).first()).toBeVisible();

  await check.getByRole('button', { name: 'Search code for send_request' }).click();
  const search = page.locator('[data-slot="code-search-check"]');
  await expect(search).toContainText('No exact match for send_request. The search found only other names that contain it, such as send_request_headers:');
  await expect(search).not.toContainText('Found send_request');
  await expect(search.getByRole('list', { name: 'Partial matches' }).getByRole('button', { name: /extensions\.md:1/ })).toBeVisible();
});

test('an answer that says it did not retrieve a name offers a one-click code search', async ({ page }) => {
  await mockApi(page);
  await routeStream(page, () => [['done', done({
    answer: `## Evidence limits\nI did not retrieve \`handleConfirmation\`, so its callers are not listed (\`${CITED_PATH}:L42-L118\`).`,
  })]]);
  await page.goto('/app/r/cal.com');
  await ask(page, 'Who calls handleConfirmation?');
  await page.getByRole('button', { name: 'Search code for handleConfirmation' }).click();
  const result = page.locator('[data-slot="code-search-check"]');
  await expect(result).toContainText('Found handleConfirmation in 1 place in the indexed code');
  await result.getByRole('button', { name: /handleConfirmation\.ts:42/ }).click();
  await expect(page.getByRole('complementary', { name: `File ${CITED_PATH}` }).first()).toBeVisible();
});

test('a failed Detailed answer names the failure and offers Retry with Fast', async ({ page }) => {
  await mockApi(page);
  const payloads = await routeStream(page, (payload) => (payload.model_profile === 'detailed' ? [['done', done({
    answer: '## Retrieved code context\nNo answer was written. The Detailed model did not respond in time.',
    mode: 'retrieval_fallback', model_profile: 'detailed', answer_references: [],
    answer_failure: { reason: 'timeout', message: 'The Detailed model did not respond in time.', model_profile: 'detailed' },
  })]] : [['done', done({ answer: `Answered quickly from \`${CITED_PATH}:L42-L118\`.` })]]));
  await page.goto('/app/r/cal.com');
  await page.getByRole('button', { name: /Answer model/ }).click();
  await page.getByRole('menuitemradio', { name: /Detailed/ }).click();
  await ask(page, 'I need a go/no-go risk summary for changing confirmation emails.');
  await expect(page.getByText('The Detailed model did not respond in time.').first()).toBeVisible();
  await page.getByRole('button', { name: 'Retry with Fast' }).click();
  await expect(page.getByText('Answered quickly from')).toBeVisible();
  expect(payloads.map((payload) => payload.model_profile)).toEqual(['detailed', 'fast']);
});

test('an answer that stops partway names why and offers Retry with Fast', async ({ page }) => {
  await mockApi(page);
  const failure = {
    reason: 'interrupted', cause: 'timeout', model_profile: 'detailed',
    message: 'The Detailed model stopped responding partway through the answer.',
  };
  const text = `## Direct answer\nConfirmation starts in \`${CITED_PATH}:L42-L118\`. Then the handler`;
  const payloads = await routeStream(page, (payload) => (payload.model_profile === 'detailed' ? [
    ['token', { text }],
    ['done', done({
      answer: `${text}\n\n> The answer was interrupted. ${failure.message} Ask again to regenerate it.`,
      mode: 'partial', model_profile: 'detailed', answer_failure: failure,
    })],
  ] : [['done', done({ answer: `Answered quickly from \`${CITED_PATH}:L42-L118\`.` })]]));
  await page.goto('/app/r/cal.com');
  await page.getByRole('button', { name: /Answer model/ }).click();
  await page.getByRole('menuitemradio', { name: /Detailed/ }).click();
  await ask(page, 'I need a go/no-go risk summary for changing confirmation emails.');

  await expect(page.getByText(failure.message)).toBeVisible();
  await expect(page.getByText('The answer stops there, so the rest is missing.')).toBeVisible();
  // Named once, in the notice: the server's note is not repeated in the reply.
  await expect(page.locator('.prose-answer').last()).not.toContainText('The answer was interrupted');
  await expect(page.locator('[data-slot="answer-check"]').last()).toContainText('So far, 1 file/line reference exists');
  await expect(page.getByRole('button', { name: 'Try again' })).toBeVisible();
  await page.getByRole('button', { name: 'Retry with Fast' }).click();
  await expect(page.getByText('Answered quickly from')).toBeVisible();
  expect(payloads.map((payload) => payload.model_profile)).toEqual(['detailed', 'fast']);
});

test('Copy reply and Copy section put commit-pinned GitHub links on the clipboard', async ({ page, context }) => {
  await context.grantPermissions(['clipboard-read', 'clipboard-write']);
  await mockApi(page);
  const answer = [
    '## Direct answer',
    `Confirmation is handled in \`${CITED_PATH}:L42-L118\`.`,
    '',
    '## Draft GitHub comment',
    `Thanks for the report. The handler at ${CITED_PATH}:L50-L60 sends the email.`,
  ].join('\n');
  await routeStream(page, () => [['done', done({
    answer,
    answer_references: [verified, { text: `${CITED_PATH}:L50-L60`, file_path: CITED_PATH, start_line: 50, end_line: 60, valid: true }],
  })]]);
  await page.goto('/app/r/cal.com');
  await ask(page);
  const link = (start, end) => `https://github.com/calcom/cal.com/blob/${SHA}/${CITED_PATH}#L${start}-L${end}`;

  await page.getByRole('button', { name: 'Copy reply' }).click();
  const reply = await page.evaluate(() => navigator.clipboard.readText());
  expect(reply).toContain(`[\`${CITED_PATH}:L42-L118\`](${link(42, 118)})`);
  expect(reply).toContain(`[\`${CITED_PATH}:L50-L60\`](${link(50, 60)})`);

  await page.getByRole('heading', { name: 'Draft GitHub comment' }).hover();
  await page.getByRole('button', { name: 'Copy section: Draft GitHub comment' }).click();
  const section = await page.evaluate(() => navigator.clipboard.readText());
  expect(section).toBe(`## Draft GitHub comment\nThanks for the report. The handler at [\`${CITED_PATH}:L50-L60\`](${link(50, 60)}) sends the email.`);
});
