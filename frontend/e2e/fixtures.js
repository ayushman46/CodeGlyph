// Shared API mocks for workspace end-to-end tests (no backend needed).
export const SHA = '54343aa685ae8f33159d2f485ec4a57bad5c574a';
const GITHUB_URL = 'https://github.com/calcom/cal.com.git';
export const CITED_PATH = 'packages/features/bookings/lib/handleConfirmation.ts';

const readyRepo = {
  id: 'r1', repo_name: 'cal.com', github_url: GITHUB_URL, owner: 'calcom', full_name: 'calcom/cal.com',
  status: 'ready', index_phase: 'ready', job_status: 'completed', is_indexing: false, queryable: true,
  chunk_count: 7421, error_message: null, message: null, created_at: '2026-10-01T00:00:00Z', updated_at: '2026-10-01T00:00:00Z',
  semantic_progress: 100, embedding_status: 'complete', keyword_files: 5600, keyword_chunks: 7421,
  total_seen_files: 7695, eligible_files: 5600, indexed_files: 5600, excluded_files: 2095, excluded_bytes: 150000000,
  excluded_reasons: { unsupported_format: 1200, translations: 45, repository_budget: 300 },
  commit_sha: SHA, indexed_at: '2026-10-01T00:00:00Z',
  selection: { candidate_files: 5900, selected_files: 5600, selected_bytes: 21000000, budget_omitted_files: 300, quota_limited: false },
  refreshing: false,
};

const indexingRepo = {
  ...readyRepo, id: 'r2', repo_name: 'fastapi', github_url: 'https://github.com/tiangolo/fastapi.git', owner: 'tiangolo',
  full_name: 'tiangolo/fastapi', status: 'chunking', index_phase: 'chunking', job_status: 'processing', is_indexing: true,
  queryable: false, chunk_count: 0, semantic_progress: 0, commit_sha: null, message: 'Reading source files (120 of 900 changed files).',
  selection: {},
};

const fileContent = Array.from({ length: 160 }, (_, index) => (
  index === 41 ? 'export async function handleConfirmation(booking: Booking) {' : `  const line${index + 1} = ${index + 1};`
)).join('\n');

const citation = {
  file_path: CITED_PATH, directory: 'packages/features/bookings/lib', start_line: 42, end_line: 118,
  content: 'export async function handleConfirmation(booking: Booking) {\n  // ...\n}', language: 'ts', symbols: ['handleConfirmation'],
  retrieval_methods: ['semantic', 'keyword'], retrieval_reasons: ['Semantically closest code in the most relevant files'],
  relevance_score: 0.51, support_status: 'source-backed', cited_in_answer: true,
  github_url: `https://github.com/calcom/cal.com/blob/${SHA}/${CITED_PATH}#L42-L118`,
};

const answer = `## Direct answer\nBooking confirmation is handled in \`${CITED_PATH}:L42-L118\`, which updates the booking and sends emails.`;

export const proposal = {
  id: '11111111-2222-3333-4444-555555555555',
  title: 'Send confirmation emails in the attendee timezone',
  summary: 'Format dates with the attendee timezone.',
  pr_body: '## Summary\nUse the attendee timezone for confirmation emails.',
  validation: ['yarn test bookings'],
  issue_reference: null,
  files: [
    {
      file_path: CITED_PATH, action: 'modify', summary: 'Use attendee timezone',
      changes: [{ old: '  const line5 = 5;', new: '  const line5 = toAttendeeTimezone(5);', reason: 'timezone' }],
      before: `${fileContent}\n`, after: `${fileContent.replace('  const line5 = 5;', '  const line5 = toAttendeeTimezone(5);')}\n`,
      base_blob_sha: 'abc123', additions: 1, deletions: 1, language: 'typescript', source: 'github',
    },
    {
      file_path: 'packages/lib/timezone/toAttendeeTimezone.ts', action: 'create', summary: 'New helper', changes: [],
      before: '', after: 'export const toAttendeeTimezone = (value) => value;\n', base_blob_sha: null,
      additions: 1, deletions: 0, language: 'typescript', source: 'generated',
    },
  ],
  stats: { files: 2, additions: 2, deletions: 1 },
  file_path: CITED_PATH,
  changes: [],
};

// ------------------------------------------------------- quality gates / CI

const delayBefore = [
  "export default async function delay(ms: number, {signal}: {signal?: AbortSignal} = {}) {",
  '\treturn new Promise<void>((resolve, reject) => {',
  '\t\tconst timeoutId = setTimeout(resolve, ms);',
  "\t\tsignal?.addEventListener('abort', () => {",
  '\t\t\tclearTimeout(timeoutId);',
  '\t\t\treject(signal.reason);',
  '\t\t});',
  '\t});',
  '}',
  '',
].join('\n');
const guard = "\tif (!Number.isFinite(ms) || ms < 0) {\n\t\tthrow new TypeError('`ms` must be a non-negative finite number');\n\t}\n";
// The model dropped the function's closing brace: esbuild reports it.
const delayAfter = delayBefore.replace('= {}) {\n', `= {}) {\n${guard}`).replace(/\t}\);\n}\n$/, '\t});\n');

/** A UUID, so the review sends it as `proposal_id`. */
export const FAILING_PROPOSAL_ID = 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee';

/** A proposal whose syntax check failed after the repair round (sindresorhus/ky delay.ts). */
export const failingProposal = {
  id: FAILING_PROPOSAL_ID,
  title: 'fix: validate the delay input',
  summary: 'Reject negative and non-finite delays.',
  pr_body: '## Summary\nReject negative and non-finite delays.\n\n## Changes\n- `source/utils/delay.ts`: guard the input.\n\n## How to verify\n- [ ] npm test',
  validation: ['npm test'],
  issue_reference: null,
  files: [{
    file_path: 'source/utils/delay.ts', action: 'modify', summary: 'Guard the input',
    changes: [{ old: '= {}) {\n\treturn new Promise', new: `= {}) {\n${guard}\treturn new Promise` }, { old: '\t});\n}\n', new: '\t});\n' }],
    before: delayBefore, after: delayAfter, base_blob_sha: 'sha-delay', additions: 3, deletions: 1, language: 'typescript', source: 'github',
    syntax: { status: 'fail', checker: 'esbuild', errors: [{ line: 12, column: 1, message: 'Unexpected end of file' }] },
  }],
  stats: { files: 1, additions: 3, deletions: 1 },
  quality: {
    status: 'fail',
    checks: [
      { id: 'syntax', label: 'Syntax', status: 'fail', detail: '1 file has syntax errors (esbuild)', files: [{ file_path: 'source/utils/delay.ts', line: 12, column: 1, message: 'Unexpected end of file' }] },
      { id: 'style', label: 'Style', status: 'pass', detail: 'Matches tabs, single quotes, semicolons', files: [] },
      { id: 'imports', label: 'Imports', status: 'pass', detail: 'No relative imports changed', files: [] },
      { id: 'tests', label: 'Tests touched', status: 'warn', detail: 'no test was added or updated; tests were not run (CI runs them on the pull request)', files: [] },
      { id: 'review', label: 'Self-review', status: 'pass', detail: 'No issues found', files: [] },
    ],
    reviewed: true,
    review_summary: 'The guard matches the request.',
    review_issues: [],
    autofixed: [{ file_path: 'source/utils/delay.ts', rule: 'trailing_whitespace', lines: 1 }],
    style: { 'source/utils/delay.ts': { indent: 'tab', indent_width: null, quotes: 'single', semicolons: 'always', eol: 'lf', final_newline: true, source: 'xo' } },
  },
  conventions: {
    pr_template_path: null, commit_style: 'conventional', commit_style_source: 'commitlint config',
    contributing_path: null, agent_instructions_path: 'AGENTS.md', dco_required: false, notes: [],
  },
  commit_message: 'fix: validate the delay input\n\nReject negative and non-finite delays.',
};

/** The editor's tool calls for `failingProposal`, with the new quality steps. */
export const failingToolCalls = [
  { tool: 'check_scope', input: { files: 1, limit: 8, mechanical: 0, symbols: {} }, result_summary: '1 file: 1 edited by the model' },
  { tool: 'read_conventions', input: { sources: ['AGENTS.md', 'package.json'] }, result_summary: 'Conventional Commits (commitlint); tabs, single quotes' },
  { tool: 'check_syntax', input: { files: ['source/utils/delay.ts'] }, result_summary: 'source/utils/delay.ts:12:1 Unexpected end of file', status: 'fail' },
  { tool: 'check_style', input: { files: ['source/utils/delay.ts'] }, result_summary: 'Removed trailing whitespace in source/utils/delay.ts' },
  { tool: 'check_imports', input: { files: ['source/utils/delay.ts'] }, result_summary: 'No relative imports changed' },
  { tool: 'check_completeness', input: { files: ['source/utils/delay.ts'] }, result_summary: 'no test was added or updated', status: 'warn' },
  { tool: 'review_patch', input: { files: ['source/utils/delay.ts'], issues: 0, fixed: 0 }, result_summary: 'No issues found' },
  { tool: 'validate_patch', input: { files: ['source/utils/delay.ts'] }, result_summary: '1 file(s), +3 −1, every hunk matched exactly once' },
  { tool: 'code_edit', input: { proposal_id: FAILING_PROPOSAL_ID }, result_summary: 'fix: validate the delay input' },
];

const checkRun = (name, conclusion) => ({
  name, source: 'check_run', status: 'completed', conclusion,
  url: `https://github.com/calcom/cal.com/actions/runs/1/job/${name.length}`,
  started_at: '2026-10-21T08:00:00Z', completed_at: '2026-10-21T08:04:00Z',
});

/** GET /github/pr-status for PR #123: one failed check, failures first. */
export const prStatusFailed = {
  pr: {
    number: 123, url: 'https://github.com/calcom/cal.com/pull/123', state: 'open', merged: false, draft: false,
    mergeable: true, mergeable_state: 'blocked', head_sha: 'f'.repeat(40), base_ref: 'main', head_repo: 'tester/cal.com',
  },
  state: 'failure',
  summary: '3 of 4 checks passed, 1 failed',
  counts: { total: 4, success: 3, failure: 1, pending: 0, neutral: 0, skipped: 0, action_required: 0 },
  checks: [checkRun('Type check', 'failure'), checkRun('Unit tests', 'success'), checkRun('Lint', 'success'), checkRun('E2E', 'success')],
  checks_url: 'https://github.com/calcom/cal.com/pull/123/checks',
  fetched_at: '2026-10-21T08:05:00+00:00',
};

/** The same pull request after a fix: every check passed. */
export const prStatusPassed = {
  ...prStatusFailed,
  pr: { ...prStatusFailed.pr, mergeable_state: 'clean' },
  state: 'success',
  summary: '4 of 4 checks passed',
  counts: { ...prStatusFailed.counts, success: 4, failure: 0 },
  checks: prStatusFailed.checks.map((check) => ({ ...check, conclusion: 'success' })),
};

const sse = (events) => `: connected\n\n${events.map(([event, data]) => `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`).join('')}`;

const json = (route, body, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });

/**
 * Mocks every /api call. `state.prStatus` (default: one failed check) is
 * what GET /github/pr-status returns, and can be changed mid-test;
 * `pushResult` adds fields to a successful push response.
 */
export const mockApi = async (page, { githubConnected = false, prStatus = prStatusFailed, pushResult = {} } = {}) => {
  const state = { github: githubConnected, pushes: [], prStatus, prStatusRequests: [] };
  await page.route('**/api/**', async (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname.replace(/^\/api/, '');
    const method = route.request().method();
    if (path === '/health') return json(route, { status: 'ok' });
    if (path === '/repos' || path === '/repos/statuses') return json(route, [readyRepo, indexingRepo]);
    if (path === '/account/usage') {
      return json(route, { plan: 'explorer', used_bytes: 21000000, quota_bytes: 200000000, used_label: '21 MB', quota_label: '200 MB', remaining_label: '179 MB' });
    }
    if (path.startsWith('/conversations/')) return json(route, { messages: [] });
    if (path === '/repos/cal.com/overview') {
      return json(route, {
        repository: readyRepo, total_files: 5600,
        tree: [{ path: 'packages', files: 4100, bytes: 0, children: [{ path: 'packages/features', files: 1200, bytes: 0, children: [] }] }, { path: 'apps', files: 1400, bytes: 0, children: [] }],
        root_files: ['package.json', 'README.md'], key_files: ['package.json'],
        languages: [{ language: 'TypeScript', files: 5000, percent: 89.3 }, { language: 'SQL', files: 600, percent: 10.7 }],
        suggested_questions: ['How does authentication work, end to end?', 'Where are the API routes defined, and how is a request handled?'],
      });
    }
    if (path === '/repos/cal.com/search') {
      return json(route, { query: url.searchParams.get('q'), results: [{ file_path: CITED_PATH, start_line: 40, end_line: 60, language: 'ts', matches: [{ line: 42, text: 'export async function handleConfirmation(booking: Booking) {' }] }] });
    }
    if (path === '/repos/cal.com/file') {
      return json(route, { path: url.searchParams.get('path'), content: fileContent, source: 'github', ref: SHA, language: 'typescript', line_count: 160, github_url: `https://github.com/calcom/cal.com/blob/${SHA}/${CITED_PATH}` });
    }
    if (path === '/query/stream' && method === 'POST') {
      const payload = route.request().postDataJSON();
      if (payload.workflow === 'editing') {
        return route.fulfill({
          status: 200,
          headers: { 'content-type': 'text/event-stream' },
          body: sse([
            ['stage', { stage: 'retrieving', detail: 'Searching the indexed code' }],
            ['citations', { citations: [citation], evidence_plan: {}, snapshot: { commit_sha: SHA, github_url: GITHUB_URL } }],
            ['stage', { stage: 'planning', detail: 'Planning the change across 3 candidate files' }],
            ['stage', { stage: 'validating', detail: 'Validating every hunk against the current files' }],
            ['done', {
              answer: `## ${proposal.title}\n\nA validated change is ready for review.`, mode: 'editing', citations: [citation],
              tool_calls: [{ tool: 'code_edit', input: { proposal_id: proposal.id }, result_summary: proposal.title }],
              latency_ms: 4200, model_profile: 'code', workflow: 'editing', evidence_plan: {},
              edit_suggestion: proposal, edit_ticket: 'v1.ticket.signature', snapshot: { commit_sha: SHA, github_url: GITHUB_URL },
            }],
          ]),
        });
      }
      return route.fulfill({
        status: 200,
        headers: { 'content-type': 'text/event-stream' },
        body: sse([
          ['stage', { stage: 'retrieving', detail: 'Searching the indexed code' }],
          ['citations', { citations: [citation], evidence_plan: {}, snapshot: { commit_sha: SHA, github_url: GITHUB_URL } }],
          ['token', { text: answer.slice(0, 40) }],
          ['token', { text: answer.slice(40) }],
          ['done', { answer, mode: 'rag', citations: [citation], tool_calls: [], latency_ms: 1300, model_profile: 'fast', workflow: 'general', evidence_plan: {}, edit_suggestion: null, edit_ticket: null, snapshot: { commit_sha: SHA, github_url: GITHUB_URL } }],
        ]),
      });
    }
    if (path === '/github/status') return json(route, state.github ? { connected: true, github_username: 'tester', updated_at: '2026-10-01T00:00:00Z' } : { connected: false, github_username: null });
    if (path === `/edits/${proposal.id}`) return json(route, { proposal, edit_ticket: 'v1.fresh.ticket', repo_name: 'cal.com' });
    if (path === '/github/push-pr' && method === 'POST') {
      state.pushes.push(route.request().postDataJSON());
      return json(route, { success: true, pr_url: 'https://github.com/calcom/cal.com/pull/123', pr_number: 123, title: proposal.title, branch_name: 'codebase-intel/send-confirmation', target_repo: 'tester/cal.com', is_fork: true, already_existed: false, ...pushResult });
    }
    if (path === '/github/pr-status' && method === 'GET') {
      state.prStatusRequests.push(Object.fromEntries(url.searchParams));
      return json(route, state.prStatus);
    }
    return json(route, { detail: `Unmocked ${method} ${path}` }, 404);
  });
  return state;
};

