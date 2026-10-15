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

const sse = (events) => `: connected\n\n${events.map(([event, data]) => `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`).join('')}`;

const json = (route, body, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });

export const mockApi = async (page, { githubConnected = false } = {}) => {
  const state = { github: githubConnected, pushes: [] };
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
      return json(route, { success: true, pr_url: 'https://github.com/calcom/cal.com/pull/123', pr_number: 123, title: proposal.title, branch_name: 'codebase-intel/send-confirmation', target_repo: 'tester/cal.com', is_fork: true, already_existed: false });
    }
    return json(route, { detail: `Unmocked ${method} ${path}` }, 404);
  });
  return state;
};

